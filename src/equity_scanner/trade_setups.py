"""Read-only market context and long watchlist signals from completed candles."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from math import isfinite
from statistics import mean, median
from typing import TYPE_CHECKING

from schwab_gateway_sdk import QuoteV1

from equity_scanner.closes import previous_trading_day
from equity_scanner.provider import GatewayEquityDataProvider, StaleGatewayDataError
from equity_scanner.time_utils import EASTERN, MARKET_OPEN, is_market_open, market_close_time

if TYPE_CHECKING:
    from equity_scanner.scan_config import EquityScanSettings
    from equity_scanner.scanner import EquitySnapshot

log = logging.getLogger(__name__)

MINUTE = dt.timedelta(minutes=1)
QUOTE_MAX_AGE = dt.timedelta(minutes=5)
ATR_SESSIONS = 14
OPENING_RANGE_MINUTES = 15
BASELINE_BARS = 10


@dataclass(frozen=True)
class Bar:
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def span(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class TapeQuote:
    level: float
    change_pct: float | None
    observed_at: str
    prior_session: bool = False


@dataclass(frozen=True)
class OpeningTape:
    as_of: str
    close: float
    vwap: float
    opening_high: float
    opening_low: float
    prior_10m_pct: float
    direction: str


@dataclass(frozen=True)
class MarketBreadth:
    advances: int
    declines: int
    unchanged: int
    total: int


@dataclass(frozen=True)
class DailyDip:
    symbol: str
    status: str
    selloff_date: str
    observed_date: str
    change_pct: float
    atr_multiple: float
    volume_multiple: float | None
    selloff_low: float
    selloff_high: float


@dataclass(frozen=True)
class Capitulation:
    symbol: str
    status: str
    flush_end: str
    observed_end: str
    low_vs_prior_close_pct: float
    volume_multiple: float
    range_multiple: float
    flush_low: float
    flush_high: float


@dataclass(frozen=True)
class SetupCoverage:
    requested: int
    unavailable: tuple[str, ...]

    @property
    def evaluated(self) -> int:
        return self.requested - len(self.unavailable)


@dataclass(frozen=True)
class TradeSetupResults:
    as_of: str
    spx: TapeQuote | None
    vix: TapeQuote | None
    spy_tape: OpeningTape | None
    spy_status: str
    breadth: MarketBreadth | None
    daily_dips: list[DailyDip]
    daily_coverage: SetupCoverage
    capitulations: list[Capitulation]
    intraday_coverage: SetupCoverage
    intraday_status: str


def _bar(candle: dict) -> Bar | None:
    try:
        bar = Bar(
            start=dt.datetime.fromtimestamp(candle["datetime"] / 1000, tz=EASTERN),
            open=float(candle["open"]),
            high=float(candle["high"]),
            low=float(candle["low"]),
            close=float(candle["close"]),
            volume=int(candle["volume"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None
    if (
        not all(isfinite(p) for p in (bar.open, bar.high, bar.low, bar.close))
        or not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        or bar.volume < 0
    ):
        return None
    return bar


def _pct(value: float, reference: float) -> float:
    return 100 * (value - reference) / reference


def _completed_daily(candles: list[dict], latest: dt.date) -> list[Bar]:
    by_session: dict[dt.date, Bar] = {}
    duplicates: set[dt.date] = set()
    for candle in candles:
        if (bar := _bar(candle)) is not None:
            day = bar.start.date()
            if day in by_session:
                duplicates.add(day)
            by_session[day] = bar
    contiguous: list[Bar] = []
    while latest in by_session and latest not in duplicates:
        contiguous.append(by_session[latest])
        latest = previous_trading_day(latest)
    return contiguous[::-1]


def _daily_dip(symbol: str, bars: list[Bar]) -> DailyDip | None:
    latest = bars[-1]
    # Prefer the most recent selloff. A prior setup carries for one completed session.
    for offset in (1, 2):
        index = len(bars) - offset
        if index <= ATR_SESSIONS:
            break
        selloff = bars[index]
        prev_close = bars[index - 1].close
        baseline = bars[index - ATR_SESSIONS - 1 : index]
        atr = mean(
            max(bar.span, abs(bar.high - prev.close), abs(bar.low - prev.close))
            for prev, bar in zip(baseline, baseline[1:])
        )
        change = _pct(selloff.close, prev_close)
        true_range = max(
            selloff.span, abs(selloff.high - prev_close), abs(selloff.low - prev_close)
        )
        if change > -3 or atr <= 0 or true_range < 1.5 * atr:
            continue
        if offset == 2 and latest.low < selloff.low:
            continue
        if offset == 2 and latest.close > selloff.high:
            status = "Follow-through"
        elif offset == 2 and latest.close > selloff.close:
            status = "Holding the low"
        elif offset == 2:
            continue
        elif selloff.span and (selloff.close - selloff.low) / selloff.span >= 0.65:
            status = "Closed off the low"
        else:
            status = "Selloff watch"
        avg_volume = mean(bar.volume for bar in baseline[1:])
        return DailyDip(
            symbol,
            status,
            selloff.start.date().isoformat(),
            latest.start.date().isoformat(),
            change,
            true_range / atr,
            selloff.volume / avg_volume if avg_volume else None,
            selloff.low,
            selloff.high,
        )
    return None


def _regular_minutes(candles: list[dict], start: dt.datetime, cutoff: dt.datetime) -> list[Bar]:
    by_start: dict[dt.datetime, Bar] = {}
    for candle in candles:
        bar = _bar(candle)
        if bar is None or not start <= bar.start <= cutoff - MINUTE:
            continue
        if bar.start.second or bar.start.microsecond:
            continue
        if bar.start in by_start:
            return []
        by_start[bar.start] = bar
    minutes = [by_start[timestamp] for timestamp in sorted(by_start)]
    if (
        not minutes
        or cutoff - (minutes[-1].start + MINUTE) > MINUTE
        or any(bar.start != start + i * MINUTE for i, bar in enumerate(minutes))
    ):
        return []
    return minutes


def _two_minute_bars(minutes: list[Bar]) -> list[Bar]:
    return [
        Bar(
            first.start,
            first.open,
            max(first.high, second.high),
            min(first.low, second.low),
            second.close,
            first.volume + second.volume,
        )
        for first, second in zip(minutes[::2], minutes[1::2])
    ]


def _capitulation(symbol: str, bars: list[Bar], prior_close: float) -> Capitulation | None:
    # A flush can be confirmed by either of the next two closes. Only show signals
    # that are still active on the most recent completed bar.
    first = max(BASELINE_BARS, len(bars) - 3)
    for index in reversed(range(first, len(bars))):
        flush = bars[index]
        baseline = bars[index - BASELINE_BARS : index]
        following = bars[index + 1 :]
        typical_range = median(bar.span for bar in baseline)
        typical_volume = median(bar.volume for bar in baseline)
        move = _pct(flush.low, prior_close)
        if (
            move > -2
            or flush.low >= min(bar.low for bar in bars[:index])
            or typical_range <= 0
            or flush.span < 1.75 * typical_range
            or typical_volume <= 0
            or flush.volume < 2 * typical_volume
            or any(bar.low < flush.low for bar in following)
        ):
            continue
        latest = bars[-1]
        if latest.close > flush.high:
            status = "Reclaim confirmed"
        elif any(bar.close > flush.high for bar in following):
            continue  # The reclaim failed by the latest close.
        elif len(following) < 2:
            status = "Flush watch"
        else:
            continue
        return Capitulation(
            symbol,
            status,
            (flush.start + 2 * MINUTE).isoformat(),
            (latest.start + 2 * MINUTE).isoformat(),
            move,
            flush.volume / typical_volume,
            flush.span / typical_range,
            flush.low,
            flush.high,
        )
    return None


def _opening_tape(minutes: list[Bar]) -> OpeningTape | None:
    if len(minutes) <= OPENING_RANGE_MINUTES:
        return None
    opening = minutes[:OPENING_RANGE_MINUTES]
    high = max(bar.high for bar in opening)
    low = min(bar.low for bar in opening)
    volume = sum(bar.volume for bar in minutes)
    if not volume:
        return None
    weighted = sum((bar.high + bar.low + bar.close) / 3 * bar.volume for bar in minutes)
    vwap = weighted / volume
    latest, prior = minutes[-1], minutes[-2]
    if latest.close > max(high, vwap):
        direction = "New upside break" if prior.close <= high else "Upside break holding"
    elif latest.close < min(low, vwap):
        direction = "New downside break" if prior.close >= low else "Downside break holding"
    elif latest.close > vwap:
        direction = "Above VWAP, breakout unconfirmed"
    elif latest.close < vwap:
        direction = "Below VWAP, breakout unconfirmed"
    else:
        direction = "At VWAP"
    return OpeningTape(
        (latest.start + MINUTE).isoformat(),
        latest.close,
        vwap,
        high,
        low,
        _pct(latest.close, minutes[-11].close),
        direction,
    )


def _fresh_quote(quote: QuoteV1, cutoff: dt.datetime) -> bool:
    stamp = quote.event_timestamp
    return (
        not quote.stale
        and stamp is not None
        and stamp.astimezone(EASTERN).date() == cutoff.date()
        and dt.timedelta(0) <= cutoff - stamp <= QUOTE_MAX_AGE
    )


def _tape_quote(quote: QuoteV1 | None, cutoff: dt.datetime) -> TapeQuote | None:
    if quote is None or quote.event_timestamp is None:
        return None
    if quote.last is None or not isfinite(quote.last) or quote.last <= 0:
        return None
    stamp = quote.event_timestamp.astimezone(EASTERN)
    prior_session = False
    if not _fresh_quote(quote, cutoff):
        if cutoff.time() >= MARKET_OPEN:
            return None
        prior_day = previous_trading_day(cutoff.date())
        prior_close = dt.datetime.combine(prior_day, market_close_time(prior_day), tzinfo=EASTERN)
        if abs(stamp - prior_close) > dt.timedelta(minutes=30):
            return None
        prior_session = True
    change = None
    if quote.close is not None and isfinite(quote.close) and quote.close > 0:
        change = _pct(quote.last, quote.close)
    elif quote.session == "regular" and quote.net_percent_change is not None:
        if isfinite(quote.net_percent_change):
            change = quote.net_percent_change
    return TapeQuote(quote.last, change, stamp.isoformat(), prior_session)


def _market_breadth(
    symbols: list[str], quotes: dict[str, QuoteV1], cutoff: dt.datetime
) -> MarketBreadth | None:
    if not symbols:
        return None
    advances = declines = unchanged = 0
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote is None or quote.session != "regular" or not _fresh_quote(quote, cutoff):
            continue
        price, close = quote.last, quote.close
        if price is None or close is None or not (
            isfinite(price) and isfinite(close) and price > 0 and close > 0
        ):
            continue
        advances += price > close
        declines += price < close
        unchanged += price == close
    return MarketBreadth(advances, declines, unchanged, len(symbols))


async def scan_trade_setups(
    provider: GatewayEquityDataProvider,
    snapshots: list[EquitySnapshot],
    *,
    quotes: dict[str, QuoteV1],
    context_quotes: dict[str, QuoteV1],
    sp500_symbols: list[str],
    known_prior_day: dict[str, float],
    settings: EquityScanSettings,
    as_of: dt.datetime,
) -> TradeSetupResults:
    cutoff = as_of.astimezone(EASTERN)
    today = cutoff.date()
    start = dt.datetime.combine(today, MARKET_OPEN, tzinfo=EASTERN)
    close = dt.datetime.combine(today, market_close_time(today), tzinfo=EASTERN)
    latest_daily = today if cutoff >= close else previous_trading_day(today)
    in_session = is_market_open(cutoff)
    elapsed = (cutoff - start) // MINUTE
    limits = settings.trade_setups

    def daily_priority(snapshot: EquitySnapshot) -> float:
        quote = quotes[snapshot.symbol]
        current = snapshot.session_gap_pct if _fresh_quote(quote, cutoff) else 0
        return min(0, known_prior_day.get(snapshot.symbol, 0), current)

    daily_rank = sorted(
        ((daily_priority(snap), snap.symbol, snap) for snap in snapshots),
        key=lambda item: (item[0], "custom" not in item[2].universes, item[1]),
    )
    daily_symbols = [
        symbol
        for priority, symbol, snap in daily_rank
        if priority <= -1 or "custom" in snap.universes
    ][: limits.daily_candidates]
    intraday_rank = sorted(snapshots, key=lambda snap: (snap.session_gap_pct, snap.symbol))
    intraday_targets = (
        [
            snap
            for snap in intraday_rank
            if (snap.session_gap_pct <= -1 or "custom" in snap.universes)
            and quotes[snap.symbol].session == "regular"
            and _fresh_quote(quotes[snap.symbol], cutoff)
        ][: limits.intraday_candidates]
        if in_session and elapsed >= 2 * (BASELINE_BARS + 1)
        else []
    )

    sem = asyncio.Semaphore(settings.rvol_fetch_concurrency)

    async def history(symbol: str, *, daily: bool = False) -> list[dict]:
        async with sem:
            try:
                if daily:
                    return await provider.get_daily_bars(
                        symbol, days_back=max(20, settings.rvol_lookback_days)
                    )
                return await provider.get_minute_bars(symbol)
            except StaleGatewayDataError:
                log.warning("equity_scan_trade_setup_stale symbol=%s daily=%s", symbol, daily)
                return []

    spy_ready = in_session and elapsed > OPENING_RANGE_MINUTES
    async with asyncio.TaskGroup() as tasks:
        daily_tasks = {
            symbol: tasks.create_task(history(symbol, daily=True)) for symbol in daily_symbols
        }
        minute_symbols = {snap.symbol for snap in intraday_targets}
        if spy_ready:
            minute_symbols.add("SPY")
        minute_tasks = {
            symbol: tasks.create_task(history(symbol)) for symbol in sorted(minute_symbols)
        }

    daily_dips: list[DailyDip] = []
    daily_missing: list[str] = []
    for symbol, task in daily_tasks.items():
        bars = _completed_daily(task.result(), latest_daily)
        if len(bars) < ATR_SESSIONS + 2:
            daily_missing.append(symbol)
        elif (dip := _daily_dip(symbol, bars)) is not None:
            daily_dips.append(dip)

    minutes = {
        symbol: _regular_minutes(task.result(), start, cutoff)
        for symbol, task in minute_tasks.items()
    }
    spy = _opening_tape(minutes["SPY"]) if spy_ready else None
    capitulations: list[Capitulation] = []
    intraday_missing: list[str] = []
    for snap in intraday_targets:
        bars = _two_minute_bars(minutes[snap.symbol])
        if len(bars) <= BASELINE_BARS:
            intraday_missing.append(snap.symbol)
        elif (signal := _capitulation(snap.symbol, bars, snap.prior_close)) is not None:
            capitulations.append(signal)

    daily_dips.sort(
        key=lambda item: (
            ("Follow-through", "Holding the low", "Closed off the low", "Selloff watch").index(
                item.status
            ),
            -item.atr_multiple,
            item.symbol,
        )
    )
    capitulations.sort(
        key=lambda item: (item.status != "Reclaim confirmed", -item.volume_multiple, item.symbol)
    )
    spy_status = (
        "available"
        if spy
        else "minute coverage unavailable"
        if spy_ready
        else "building the 15-minute opening range"
        if in_session
        else "outside the cash session"
    )
    intraday_status = (
        "screened"
        if intraday_targets
        else "outside the cash session"
        if not in_session
        else "building the 2-minute baseline"
        if elapsed < 2 * (BASELINE_BARS + 1)
        else "no eligible fresh quotes"
    )
    return TradeSetupResults(
        as_of=cutoff.isoformat(),
        spx=_tape_quote(context_quotes.get("$SPX"), cutoff),
        vix=_tape_quote(context_quotes.get("$VIX"), cutoff),
        spy_tape=spy,
        spy_status=spy_status,
        breadth=_market_breadth(sp500_symbols, quotes, cutoff) if in_session else None,
        daily_dips=daily_dips,
        daily_coverage=SetupCoverage(len(daily_symbols), tuple(daily_missing)),
        capitulations=capitulations,
        intraday_coverage=SetupCoverage(len(intraday_targets), tuple(intraday_missing)),
        intraday_status=intraday_status,
    )
