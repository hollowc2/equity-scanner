"""Read-only SPX market snapshot for the ButterflyGuy entry window."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, TypeVar

from schwab_gateway_sdk import QuoteV1
from schwab_gateway_sdk.client import (
    GatewayRateLimitError,
    GatewayResponseError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)
from schwab_gateway_sdk.models import OptionChainV1, OptionContractV1

from equity_scanner.closes import previous_trading_day
from equity_scanner.config import AppSettings
from equity_scanner.files import atomic_write_text
from equity_scanner.gateway import build_gateway_client
from equity_scanner.notifier import DiscordNotifier
from equity_scanner.provider import GatewayEquityDataProvider, StaleGatewayDataError
from equity_scanner.report import format_market_breadth
from equity_scanner.scan_config import load_equity_scan_config
from equity_scanner.time_utils import (
    EASTERN,
    MARKET_OPEN,
    is_market_open,
    market_close_time,
    now_eastern,
)
from equity_scanner.trade_setups import (
    MarketBreadth,
    OpeningTape,
    TapeQuote,
    _bar,
    _completed_daily,
    _fresh_quote,
    _market_breadth,
    _opening_tape,
    _regular_minutes,
    _tape_quote,
)
from equity_scanner.universes import load_universes

log = logging.getLogger(__name__)
T = TypeVar("T")

# ButterflyGuy SPX config at b3150ad and butterfly_builder._BUCKET_SIGMAS.
VIX_BUCKETS = (
    (17.0, "Zombieland", (20, 25, 30)),
    (24.5, "Goldilocks 1", (20, 30, 40)),
    (32.0, "Goldilocks 2", (40, 45, 50)),
    (math.inf, "Chaos", (50, 55, 65)),
)
VOLATILITY_SYMBOLS = ("$VIX", "$VIX1D", "$VIX3M")
MAX_CHAIN_AGE = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class QuotedPrice:
    strike: float
    midpoint: float
    ask: float

    @property
    def ask_premium_pct(self) -> float:
        return 100 * (self.ask / self.midpoint - 1)


@dataclass(frozen=True)
class WidthContext:
    width: int
    target: float | None
    sample: QuotedPrice | None


@dataclass(frozen=True)
class SpxSnapshot:
    as_of: str
    prior_session: str
    spot: TapeQuote | None
    previous_close: float | None
    cash_open: float | None
    opening_gap_points: float | None
    opening_gap_pct: float | None
    gap_status: str | None
    gap_filled: bool | None
    gap_direction: str | None
    vix: TapeQuote | None
    vix_bucket: str | None
    vix_move_points: float | None
    width_context: tuple[WidthContext, ...]
    prior_vix_closes: dict[str, float | None]
    prior_vix1d_vix: float | None
    prior_vix1d_source: str | None
    prior_vix_vix3m: float | None
    spy_tape: OpeningTape | None
    breadth: MarketBreadth | None
    chain_as_of: str | None
    straddle: QuotedPrice | None


async def _optional_read(label: str, read: Awaitable[T]) -> T | None:
    try:
        return await read
    except GatewayRateLimitError:
        raise
    except (
        GatewayResponseError,
        GatewayTimeoutError,
        GatewayUnavailableError,
        StaleGatewayDataError,
    ) as exc:
        log.warning(
            "spx_snapshot_data_unavailable field=%s error_type=%s", label, type(exc).__name__
        )
        return None


def _session_open(candles: list[dict], start: dt.datetime) -> float | None:
    opens = [bar.open for candle in candles if (bar := _bar(candle)) and bar.start == start]
    return opens[0] if len(opens) == 1 else None


def _previous_close(candles: list[dict], date: dt.date) -> float | None:
    bars = _completed_daily(candles, date)
    return bars[-1].close if bars else None


def _vix1d_previous_quote(
    quote: QuoteV1 | None,
    prior: dt.date,
    cutoff: dt.datetime,
) -> tuple[float | None, str | None]:
    if quote is None or quote.event_timestamp is None or quote.session != "regular":
        return None, None
    if _fresh_quote(quote, cutoff):
        value = quote.close
        source = "gateway previous close"
    else:
        stamp = quote.event_timestamp.astimezone(EASTERN)
        closing = dt.datetime.combine(prior, market_close_time(prior), tzinfo=EASTERN)
        closing += dt.timedelta(minutes=15)
        if abs(stamp - closing) > dt.timedelta(seconds=90):
            return None, None
        value = quote.last
        source = "prior-session closing print"
    return (
        (value, source)
        if value is not None and math.isfinite(value) and value > 0
        else (
            None,
            None,
        )
    )


def _gap_status(
    cash_open: float | None, prior_close: float | None, spot: TapeQuote | None
) -> tuple[float | None, float | None, str | None, str | None]:
    if cash_open is None or prior_close is None:
        return None, None, None, None
    gap = cash_open - prior_close
    direction = "CALL" if gap >= 0 else "PUT"
    if not gap:
        state = "Flat open"
    elif spot is None:
        state = None
    elif (spot.level - prior_close) * gap <= 0:
        state = "At or through prior close"
    elif (spot.level - cash_open) * gap < 0:
        state = "Back inside the gap"
    else:
        state = "At or beyond the cash open"
    return gap, 100 * gap / prior_close, direction, state


def _option_quotes(
    chain: OptionChainV1 | None,
    cutoff: dt.datetime,
) -> tuple[dict[tuple[str, float], OptionContractV1], str | None]:
    if chain is None or chain.stale or chain.expiration != cutoff.date():
        return {}, None
    stamp = chain.event_timestamp
    if stamp is None or not dt.timedelta(0) <= cutoff - stamp <= MAX_CHAIN_AGE:
        return {}, None
    quotes = {}
    for option in chain.contracts:
        quote_time = option.event_timestamp
        if (
            not option.symbol.startswith("SPXW ")
            or option.expiration != cutoff.date()
            or option.stale
            or quote_time is None
            or not dt.timedelta(0) <= cutoff - quote_time <= MAX_CHAIN_AGE
            or "crossed_market_normalized" in option.data_quality_flags
            or not (math.isfinite(option.bid) and math.isfinite(option.ask))
            or not 0 <= option.bid <= option.ask
            or option.ask <= 0
        ):
            continue
        quotes[option.option_type, option.strike] = option
    return quotes, stamp.astimezone(EASTERN).isoformat()


def _quote_mid(option: OptionContractV1) -> float:
    return (option.bid + option.ask) / 2


def _atm_straddle(
    quotes: dict[tuple[str, float], OptionContractV1],
    spot: float,
) -> QuotedPrice | None:
    strikes = {strike for side, strike in quotes if side == "CALL" and ("PUT", strike) in quotes}
    if not strikes:
        return None
    strike = min(strikes, key=lambda price: (abs(price - spot), price))
    if abs(strike - spot) > 5:
        return None
    call, put = quotes["CALL", strike], quotes["PUT", strike]
    return QuotedPrice(strike, _quote_mid(call) + _quote_mid(put), call.ask + put.ask)


def _fly_sample(
    quotes: dict[tuple[str, float], OptionContractV1],
    side: str,
    spot: float,
    target: float,
    width: int,
) -> QuotedPrice | None:
    centers = sorted(
        (
            strike
            for q_side, strike in quotes
            if q_side == side
            and abs(strike - target) <= 15
            and (strike > spot if side == "CALL" else strike < spot)
        ),
        key=lambda strike: (abs(strike - target), strike),
    )
    for center in centers:
        lower, body, upper = (
            quotes.get((side, x)) for x in (center - width, center, center + width)
        )
        if lower is None or body is None or upper is None:
            continue
        mid = _quote_mid(lower) + _quote_mid(upper) - 2 * _quote_mid(body)
        ask = lower.ask + upper.ask - 2 * body.bid
        if mid >= 0.05 and ask >= mid:
            return QuotedPrice(center, mid, ask)
    return None


def _vix_context(
    vix: TapeQuote | None,
    spot: TapeQuote | None,
    direction: str | None,
    quotes: dict[tuple[str, float], OptionContractV1],
) -> tuple[str | None, float | None, tuple[WidthContext, ...]]:
    if vix is None:
        return None, None, ()
    _, bucket, widths = next(row for row in VIX_BUCKETS if vix.level < row[0])
    if spot is None:
        return bucket, None, tuple(WidthContext(width, None, None) for width in widths)
    move = spot.level * vix.level / 100 / math.sqrt(252)
    if direction is None:
        return bucket, move, tuple(WidthContext(width, None, None) for width in widths)
    sign = 1 if direction == "CALL" else -1
    contexts = []
    for width, sigma in zip(widths, (0.25, 0.50, 0.75), strict=True):
        target = round((spot.level + sign * sigma * move) / 5) * 5
        sample = _fly_sample(quotes, direction, spot.level, target, width)
        contexts.append(WidthContext(width, target, sample))
    return bucket, move, tuple(contexts)


async def collect_spx_snapshot(
    provider: GatewayEquityDataProvider,
    started: dt.datetime,
    sp500_symbols: list[str] | None = None,
) -> SpxSnapshot:
    session = started.date()
    prior = previous_trading_day(session)
    start = dt.datetime.combine(session, MARKET_OPEN, tzinfo=EASTERN)
    prior_closes = {}
    for symbol in ("$SPX", *VOLATILITY_SYMBOLS):
        bars = await _optional_read(symbol, provider.get_daily_bars(symbol, days_back=2))
        prior_closes[symbol] = _previous_close(bars or [], prior)
    spx_candles = await _optional_read(
        "$SPX minute",
        provider.get_session_minute_bars("$SPX", session),
    )
    spy_candles = await _optional_read(
        "SPY minute",
        provider.get_session_minute_bars("SPY", session),
    )
    cash_open = _session_open(spx_candles or [], start)
    breadth_quotes = None
    if sp500_symbols:
        breadth_quotes = await _optional_read(
            "S&P 500 breadth",
            provider.get_equity_quote_collection(
                sp500_symbols,
                concurrency=1,
                max_unquotable_symbols=10,
            ),
        )
    market = await provider.get_equity_quote_collection(
        ["$SPX", "$VIX", "$VIX1D", "SPY"],
        max_unquotable_symbols=2,
    )
    chain = await _optional_read("SPXW chain", provider.get_option_chain("$SPX", session))
    cutoff = now_eastern()
    spot = _tape_quote(market.quotes.get("$SPX"), cutoff)
    vix = _tape_quote(market.quotes.get("$VIX"), cutoff)
    previous_close = prior_closes.pop("$SPX")
    vix1d_source = "gateway daily candle" if prior_closes["$VIX1D"] is not None else None
    if vix1d_source is None:
        prior_closes["$VIX1D"], vix1d_source = _vix1d_previous_quote(
            market.quotes.get("$VIX1D"),
            prior,
            cutoff,
        )
    gap, gap_pct, direction, gap_status = _gap_status(cash_open, previous_close, spot)
    spx_minutes = _regular_minutes(spx_candles or [], start, cutoff)
    gap_filled = None
    if gap and previous_close is not None:
        if gap_status == "At or through prior close":
            gap_filled = True
        elif spx_minutes:
            gap_filled = any(
                bar.low <= previous_close if gap > 0 else bar.high >= previous_close
                for bar in spx_minutes
            )
    quotes, chain_time = _option_quotes(chain, cutoff)
    bucket, move, widths = _vix_context(vix, spot, direction, quotes)
    prior_vix, prior_vix1d, prior_vix3m = (prior_closes[symbol] for symbol in VOLATILITY_SYMBOLS)
    return SpxSnapshot(
        as_of=cutoff.isoformat(),
        prior_session=prior.isoformat(),
        spot=spot,
        previous_close=previous_close,
        cash_open=cash_open,
        opening_gap_points=gap,
        opening_gap_pct=gap_pct,
        gap_status=gap_status,
        gap_filled=gap_filled,
        gap_direction=direction,
        vix=vix,
        vix_bucket=bucket,
        vix_move_points=move,
        width_context=widths,
        prior_vix_closes=prior_closes,
        prior_vix1d_vix=prior_vix1d / prior_vix if prior_vix1d and prior_vix else None,
        prior_vix1d_source=vix1d_source,
        prior_vix_vix3m=prior_vix / prior_vix3m if prior_vix and prior_vix3m else None,
        spy_tape=_opening_tape(_regular_minutes(spy_candles or [], start, cutoff)),
        breadth=_market_breadth(
            sp500_symbols or [],
            breadth_quotes.quotes if breadth_quotes else {},
            cutoff,
        ),
        chain_as_of=chain_time,
        straddle=_atm_straddle(quotes, spot.level) if spot else None,
    )


def _number(value: float | None, fmt: str = ",.2f") -> str:
    return format(value, fmt) if value is not None else "unavailable"


def format_spx_snapshot(result: SpxSnapshot) -> str:
    at = dt.datetime.fromisoformat(result.as_of)
    lines = [
        f"## SPX Butterfly Snapshot · {at:%a %b %d · %H:%M ET}",
        f"**SPX** {_number(result.spot.level if result.spot else None)} · "
        f"cash open {_number(result.cash_open)} · prior close {_number(result.previous_close)}",
        f"**Opening gap** {_number(result.opening_gap_points, '+.2f')} pts · "
        f"{_number(result.opening_gap_pct, '+.2f')}% · "
        f"{result.gap_direction or 'direction unavailable'} · "
        f"{result.gap_status or 'status unavailable'}",
        f"**VIX** {_number(result.vix.level if result.vix else None)} · "
        f"{result.vix_bucket or 'bucket unavailable'} · "
        f"1σ daily estimate ±{_number(result.vix_move_points)} SPX pts",
        f"**Prior session ({result.prior_session})** · "
        f"VIX1D/VIX {_number(result.prior_vix1d_vix, '.3f')} · "
        f"VIX/VIX3M {_number(result.prior_vix_vix3m, '.3f')}",
    ]
    if result.gap_filled is not None:
        status = "filled since the open" if result.gap_filled else "still unfilled"
        lines.insert(3, f"-# Opening gap {status}")
    if result.vix:
        lines.append(
            f"-# VIX print {dt.datetime.fromisoformat(result.vix.observed_at):%H:%M:%S ET}"
        )
    if result.prior_vix1d_source:
        lines.append(f"-# Prior VIX1D source · {result.prior_vix1d_source}")
    if result.spy_tape:
        tape = result.spy_tape
        lines.append(f"**SPY proxy** · {tape.direction} · est. VWAP {tape.vwap:.2f}")
    if result.breadth:
        lines.append(format_market_breadth(result.breadth))
    if result.straddle:
        price = result.straddle
        lines.append(
            f"**0DTE SPXW straddle** · {price.strike:,.0f} strike · "
            f"{price.midpoint:.2f} mid / {price.ask:.2f} ask pts"
        )
    else:
        lines.append("**0DTE SPXW straddle** · fresh paired quotes unavailable")
    for context in result.width_context:
        detail = "fresh symmetric legs unavailable"
        if price := context.sample:
            detail = (
                f"{price.strike:,.0f} quoted center · {price.midpoint:.2f} mid / "
                f"{price.ask:.2f} ask · ask {price.ask_premium_pct:+.1f}% vs mid"
            )
        target = _number(context.target, ",.0f")
        lines.append(f"**{context.width}-wide** · target {target} · {detail}")
    return "\n".join(lines)


async def run_spx_snapshot(scan_config_path: str, *, dry_run: bool) -> None:
    started = now_eastern()
    if not is_market_open(started) or started.time() < dt.time(9, 31):
        log.info("spx_snapshot_skipped reason=outside_cash_data_window")
        return
    settings = AppSettings()
    scan_config = load_equity_scan_config(scan_config_path)
    universes = load_universes(
        ["sp500"],
        universe_dir=scan_config.universe_dir,
        custom_watchlist=scan_config.custom_watchlist,
    )
    async with build_gateway_client(settings) as gateway:
        result = await collect_spx_snapshot(
            GatewayEquityDataProvider(gateway, max_attempts=1),
            started,
            universes.get("sp500"),
        )
    report = format_spx_snapshot(result)
    directory = Path(scan_config.report_dir) / "spx_snapshots"
    stem = started.strftime("%Y-%m-%d_%H%M%S_%f")
    atomic_write_text(directory / f"{stem}.md", report + "\n")
    atomic_write_text(directory / f"{stem}.json", json.dumps(asdict(result), indent=2) + "\n")
    if dry_run:
        print(report)
    else:
        if not settings.discord_webhook_url:
            raise RuntimeError("EquityScanner Discord destination is missing")
        notifier = DiscordNotifier(settings.discord_webhook_url.get_secret_value())
        await notifier.notify_messages([report])
    log.info("spx_snapshot_complete dry_run=%s archive=%s", dry_run, stem)
