"""Core scan-ranking logic, ported from ButterflyGuy's equity_scan/scanner.py.

This logic is provider-agnostic in the original (it takes dicts/dataclasses, not a
Schwab client) — that's what makes this port mechanical, with one exception: quote
parsing. ButterflyGuy fed `parse_equity_quote` a raw two-session Schwab payload
(`{"quote": {...regular...}, "extended": {...premarket...}}`) and picked the fresher
side itself via `_price_choice`. SchwabGateway's `/v1/quotes` returns a flat,
already-session-resolved `QuoteV1` instead — the gateway does that freshness pick
server-side (see SchwabGateway's `normalize_schwab_quote`) and reports the winner in
`quote.session`, plus `quote.close`/`quote.net_percent_change` sourced specifically
from the regular session regardless of which session won. `_price_choice` and
`parse_equity_quote` are rewritten around that flat shape below; two real behavior
changes fall out of it (documented on `parse_equity_quote`), not just a mechanical
port.

News/catalyst-watch support (dropped in Phase 1 as explicitly out of scope) is
restored here: the `news` field on `EquitySnapshot`, `attach_news_impacts`,
`rank_catalyst_watch`, and the news-scoring term in opening-focus ranking.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Any

from schwab_gateway_sdk import QuoteV1

from equity_scanner.news import NewsImpact
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.time_utils import is_market_open, is_premarket_window

INDEX_UNIVERSES = frozenset({"sp500", "nq100"})


@dataclass(frozen=True)
class EquitySnapshot:
    symbol: str
    price: float
    prior_close: float
    prior_day_pct: float
    session_gap_pct: float
    volume: int
    premarket_volume: int
    avg_volume_20d: float | None
    rvol: float | None
    sector: str
    universes: tuple[str, ...]
    price_source: str = "unknown"
    price_time_ms: int | None = None
    quote_age_seconds: float | None = None
    reference_price: float | None = None
    data_quality_flags: tuple[str, ...] = ()
    news: NewsImpact | None = None


@dataclass(frozen=True)
class OpeningFocusItem:
    snapshot: EquitySnapshot
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    price: float
    change_pct: float


@dataclass(frozen=True)
class ScanResults:
    opening_focus: list[OpeningFocusItem]
    catalyst_watch: list[EquitySnapshot]
    prior_gainers: list[EquitySnapshot]
    prior_losers: list[EquitySnapshot]
    premarket_gainers: list[EquitySnapshot]
    premarket_losers: list[EquitySnapshot]
    movers_up: list[dict[str, Any]]
    movers_down: list[dict[str, Any]]
    market_context: list[MarketContext]
    scanned_symbols: int
    matched_symbols: int
    show_premarket: bool = True
    show_movers: bool = True
    rejected_symbols: dict[str, int] | None = None
    bad_data: list[dict[str, Any]] | None = None
    phase_timings_ms: dict[str, float] | None = None
    quote_coverage: dict[str, Any] | None = None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _event_time_ms(event_timestamp: dt.datetime | None) -> int | None:
    if event_timestamp is None:
        return None
    return int(event_timestamp.timestamp() * 1000)


def _quote_age_seconds(
    trade_time_ms: int | None,
    generated_at: dt.datetime | None,
) -> float | None:
    if not trade_time_ms or generated_at is None:
        return None
    ts = dt.datetime.fromtimestamp(trade_time_ms / 1000.0, tz=dt.timezone.utc)
    return max(0.0, (generated_at.astimezone(dt.timezone.utc) - ts).total_seconds())


def _price_choice(quote: QuoteV1) -> tuple[float | None, str, int | None]:
    """Pick the best current price from a flat, already session-resolved gateway
    quote. The gateway has already chosen the fresher of regular/extended and
    reports the winner in `quote.session` — there is no separate regular-vs-extended
    comparison left to do here, unlike ButterflyGuy's two-payload `_price_choice`."""
    session = quote.session or "unknown"
    price_time_ms = _event_time_ms(quote.event_timestamp)
    # Explicit `is not None and > 0` rather than truthy checks: a truthy check
    # silently accepts a bad negative price (only 0.0 is falsy) and treats it as
    # good data, where falling through to a better source is what we actually want.
    if quote.last is not None and quote.last > 0:
        return quote.last, f"{session}.last", price_time_ms
    if quote.mark is not None and quote.mark > 0:
        return quote.mark, f"{session}.mark", price_time_ms
    if quote.bid is not None and quote.bid > 0 and quote.ask is not None and quote.ask > 0:
        return (quote.bid + quote.ask) / 2.0, f"{session}.bid_ask_mid", price_time_ms
    return None, session, price_time_ms


def parse_equity_quote(
    symbol: str,
    quote: QuoteV1,
    *,
    universes: set[str],
    sector: str = "Unknown",
    avg_volume_20d: float | None = None,
    in_premarket: bool = False,
    generated_at: dt.datetime | None = None,
    reference_price: float | None = None,
    max_price_disagreement_pct: float | None = None,
    max_reference_price_deviation_pct: float | None = None,
    prior_day_pct_override: float | None = None,
    reject_reasons: list[dict[str, Any]] | None = None,
) -> EquitySnapshot | None:
    """Normalize a gateway QuoteV1 into an EquitySnapshot.

    Two behavior changes vs. ButterflyGuy's original, both consequences of the flat
    quote shape (see module docstring):

    - The regular-move-vs-official-netPercentChange sanity check
      (`max_price_disagreement_pct`) only runs when `quote.session == "regular"`,
      since a separate regular-session price to compare against is no longer
      available once the gateway has resolved to the extended session. During
      premarket this guard is unavailable — a real coverage loss, not a bug.
    - `volume`/`premarket_volume` come from the single volume figure the gateway
      reports for whichever session won, not a simultaneous regular+extended pair.
      During premarket, `volume` reflects extended-session cumulative volume, not
      the prior full trading day's total the way it did before.
    """
    prior_close = quote.close
    if prior_close is None or prior_close <= 0:
        if reject_reasons is not None:
            reject_reasons.append({"symbol": symbol, "reason": "missing_prior_close"})
        return None

    price, price_source, price_time_ms = _price_choice(quote)
    if price is None or price <= 0:
        if reject_reasons is not None:
            reject_reasons.append({"symbol": symbol, "reason": "missing_live_price"})
        return None

    prior_day_pct = quote.net_percent_change
    if prior_day_pct is None:
        prior_day_pct = (price - prior_close) / prior_close * 100.0

    session_gap_pct = (price - prior_close) / prior_close * 100.0
    flags: list[str] = list(quote.data_quality_flags)
    if quote.stale and "gateway_stale" not in flags:
        flags.append("gateway_stale")
    if (
        quote.session == "regular"
        and quote.net_percent_change is not None
        and max_price_disagreement_pct is not None
    ):
        regular_move_pct = (price - prior_close) / prior_close * 100.0
        if abs(regular_move_pct - quote.net_percent_change) > max_price_disagreement_pct:
            if reject_reasons is not None:
                reject_reasons.append(
                    {
                        "symbol": symbol,
                        "reason": "quote_percent_disagreement",
                        "regular_move_pct": regular_move_pct,
                        "net_percent_change": quote.net_percent_change,
                        "price": price,
                        "prior_close": prior_close,
                    }
                )
            return None
    if prior_day_pct_override is not None:
        prior_day_pct = prior_day_pct_override
    if reference_price and max_reference_price_deviation_pct is not None:
        reference_deviation_pct = abs(price - reference_price) / reference_price * 100.0
        if reference_deviation_pct > max_reference_price_deviation_pct:
            if reject_reasons is not None:
                reject_reasons.append(
                    {
                        "symbol": symbol,
                        "reason": "reference_price_deviation",
                        "reference_deviation_pct": reference_deviation_pct,
                        "price": price,
                        "reference_price": reference_price,
                        "price_source": price_source,
                    }
                )
            return None

    volume = _as_int(quote.volume)
    # Gated on in_premarket, not just session=="extended": the gateway reports
    # "extended" whenever extended is fresher than regular, which is true after the
    # 4pm close as much as before the 9:30am open — without this gate, an after-hours
    # run would mislabel post-close volume as a premarket rvol/gap signal.
    premarket_volume = volume if (in_premarket and quote.session == "extended") else 0
    if in_premarket and quote.session == "extended" and premarket_volume <= 0:
        flags.append("extended_price_without_volume")
    rvol = _compute_rvol(premarket_volume, avg_volume_20d)

    return EquitySnapshot(
        symbol=symbol,
        price=price,
        prior_close=prior_close,
        prior_day_pct=prior_day_pct,
        session_gap_pct=session_gap_pct,
        volume=volume,
        premarket_volume=premarket_volume,
        avg_volume_20d=avg_volume_20d,
        rvol=rvol,
        sector=sector,
        universes=tuple(sorted(universes)),
        price_source=price_source,
        price_time_ms=price_time_ms,
        quote_age_seconds=_quote_age_seconds(price_time_ms, generated_at),
        reference_price=reference_price,
        data_quality_flags=tuple(flags),
    )


def _compute_rvol(premarket_volume: int, avg_volume: float | None) -> float | None:
    if avg_volume is None or avg_volume <= 0 or premarket_volume <= 0:
        return None
    return premarket_volume / avg_volume


def passes_filters(snapshot: EquitySnapshot, settings: EquityScanSettings) -> bool:
    filters = settings.filters
    if snapshot.price < filters.min_price or snapshot.volume < filters.min_volume:
        return False
    universe_set = set(snapshot.universes)
    if (
        filters.require_index_membership
        and "custom" not in universe_set
        and not (INDEX_UNIVERSES & universe_set)
    ):
        return False
    if filters.max_abs_pct is not None:
        cap = filters.max_abs_pct
        if abs(snapshot.prior_day_pct) > cap or abs(snapshot.session_gap_pct) > cap:
            return False
    if filters.min_rvol > 0 and snapshot.premarket_volume > 0:
        return snapshot.rvol is not None and snapshot.rvol >= filters.min_rvol
    return True


def _mover_change_pct(item: dict[str, Any]) -> float | None:
    pct = item.get("changePercent") or item.get("netPercentChange")
    if pct is None:
        change = item.get("change") or item.get("netChange")
        if change is None:
            return None
        try:
            return float(change)
        except (TypeError, ValueError):
            return None
    try:
        return float(pct)
    except (TypeError, ValueError):
        return None


def _mover_symbol(item: dict[str, Any]) -> str:
    return str(item.get("symbol") or item.get("ticker") or "?")


def filter_movers(
    movers_up: list[dict[str, Any]],
    movers_down: list[dict[str, Any]],
    *,
    min_abs_pct: float,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop stale/trivial movers and collapse identical up/down lists."""

    def _filter(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            pct = _mover_change_pct(item)
            if pct is None or abs(pct) < min_abs_pct:
                continue
            symbol = _mover_symbol(item)
            if symbol in seen:
                continue
            seen.add(symbol)
            filtered.append(item)
        return filtered[:limit]

    up = _filter(movers_up)
    down = _filter(movers_down)
    up_symbols = {_mover_symbol(item) for item in up}
    down_symbols = {_mover_symbol(item) for item in down}
    if up_symbols and up_symbols == down_symbols:
        return [], []
    return up, down


def build_snapshots(
    quotes: dict[str, QuoteV1],
    symbol_map: dict[str, set[str]],
    settings: EquityScanSettings,
    *,
    avg_volumes: dict[str, float] | None = None,
    sector_map: dict[str, str] | None = None,
    reference_prices: dict[str, float] | None = None,
    prior_day_changes: dict[str, float] | None = None,
    in_premarket: bool = False,
    generated_at: dt.datetime | None = None,
    rejected_symbols: dict[str, int] | None = None,
    bad_data: list[dict[str, Any]] | None = None,
) -> list[EquitySnapshot]:
    avg_volumes = avg_volumes or {}
    sector_map = sector_map or {}
    reference_prices = reference_prices or {}
    prior_day_changes = prior_day_changes or {}
    snapshots: list[EquitySnapshot] = []
    for symbol, quote in quotes.items():
        universes = symbol_map.get(symbol)
        if not universes:
            continue
        reject_reasons: list[dict[str, Any]] = []
        snapshot = parse_equity_quote(
            symbol,
            quote,
            universes=universes,
            sector=sector_map.get(symbol, "Unknown"),
            avg_volume_20d=avg_volumes.get(symbol),
            in_premarket=in_premarket,
            generated_at=generated_at,
            reference_price=reference_prices.get(symbol),
            max_price_disagreement_pct=settings.filters.max_price_disagreement_pct,
            max_reference_price_deviation_pct=settings.filters.max_reference_price_deviation_pct,
            prior_day_pct_override=prior_day_changes.get(symbol),
            reject_reasons=reject_reasons,
        )
        if snapshot is None:
            if rejected_symbols is not None:
                for item in reject_reasons or [{"reason": "parse_failed"}]:
                    reason = str(item.get("reason", "parse_failed"))
                    rejected_symbols[reason] = rejected_symbols.get(reason, 0) + 1
            if bad_data is not None:
                bad_data.extend(reject_reasons)
            continue
        if passes_filters(snapshot, settings):
            snapshots.append(snapshot)
        elif rejected_symbols is not None:
            rejected_symbols["filter_failed"] = rejected_symbols.get("filter_failed", 0) + 1
    return snapshots


def attach_news_impacts(
    snapshots: list[EquitySnapshot],
    news_impacts: dict[str, NewsImpact],
) -> list[EquitySnapshot]:
    """Attach catalyst metadata without changing quote normalization."""
    if not news_impacts:
        return snapshots
    return [
        replace(snapshot, news=news_impacts.get(snapshot.symbol, snapshot.news))
        for snapshot in snapshots
    ]


def _focus_reasons(
    snapshot: EquitySnapshot,
    settings: EquityScanSettings,
    *,
    sector_counts: dict[str, int],
) -> tuple[str, ...]:
    filters = settings.filters
    reasons: list[str] = []
    gap_ok = abs(snapshot.session_gap_pct) >= filters.premarket_min_gap_pct
    prior_ok = abs(snapshot.prior_day_pct) >= filters.prior_day_min_pct
    volume_ok = (
        (snapshot.rvol is not None and snapshot.rvol >= max(filters.min_rvol, 0.05))
        or snapshot.premarket_volume > 0
    )
    if gap_ok and volume_ok:
        reasons.append("gap with volume")
    if prior_ok and gap_ok and snapshot.prior_day_pct * snapshot.session_gap_pct > 0:
        reasons.append("continuation setup")
    if (
        abs(snapshot.prior_day_pct) >= filters.prior_day_min_pct
        and abs(snapshot.session_gap_pct) >= filters.premarket_min_gap_pct
        and snapshot.prior_day_pct * snapshot.session_gap_pct < 0
    ):
        reasons.append("fade risk")
    if "custom" in snapshot.universes and (gap_ok or prior_ok):
        reasons.append("custom watchlist")
    if sector_counts.get(snapshot.sector, 0) >= 2 and snapshot.sector != "Unknown":
        reasons.append("sector cluster")
    if snapshot.data_quality_flags:
        reasons.append("data flag")
    if snapshot.news is not None and snapshot.news.score >= settings.news.min_score_for_focus:
        reasons.append("news catalyst")
    return tuple(reasons)


def rank_opening_focus(
    snapshots: list[EquitySnapshot],
    *,
    settings: EquityScanSettings,
) -> list[OpeningFocusItem]:
    """Rank names that deserve attention near or after the open."""
    sector_counts: dict[str, int] = {}
    for snapshot in snapshots:
        if (
            abs(snapshot.session_gap_pct) >= settings.filters.premarket_min_gap_pct
            or abs(snapshot.prior_day_pct) >= settings.filters.prior_day_min_pct
        ):
            sector_counts[snapshot.sector] = sector_counts.get(snapshot.sector, 0) + 1

    items: list[OpeningFocusItem] = []
    for snapshot in snapshots:
        reasons = _focus_reasons(snapshot, settings, sector_counts=sector_counts)
        if not reasons:
            continue
        rvol_score = min(snapshot.rvol or 0.0, 2.0) * 10.0
        custom_score = 6.0 if "custom" in snapshot.universes else 0.0
        index_score = 2.0 if INDEX_UNIVERSES & set(snapshot.universes) else 0.0
        sector_score = 3.0 if "sector cluster" in reasons else 0.0
        news_score = snapshot.news.score if snapshot.news is not None else 0.0
        score = (
            abs(snapshot.session_gap_pct) * 2.0
            + abs(snapshot.prior_day_pct)
            + rvol_score
            + custom_score
            + index_score
            + sector_score
            + news_score
        )
        items.append(OpeningFocusItem(snapshot=snapshot, score=score, reasons=reasons))
    return sorted(items, key=lambda item: item.score, reverse=True)[: settings.limits.opening_focus]


def rank_catalyst_watch(
    snapshots: list[EquitySnapshot],
    *,
    settings: EquityScanSettings,
) -> list[EquitySnapshot]:
    catalysts = [snapshot for snapshot in snapshots if snapshot.news is not None]
    return sorted(
        catalysts,
        key=lambda snap: (
            snap.news.score if snap.news is not None else 0.0,
            abs(snap.session_gap_pct),
            abs(snap.prior_day_pct),
        ),
        reverse=True,
    )[: settings.limits.catalyst_watch]


def _top(
    snapshots: list[EquitySnapshot],
    *,
    key: str,
    reverse: bool,
    min_abs_pct: float,
    limit: int,
) -> list[EquitySnapshot]:
    filtered = [
        snap
        for snap in snapshots
        if (getattr(snap, key) >= min_abs_pct if reverse else getattr(snap, key) <= -min_abs_pct)
    ]
    return sorted(filtered, key=lambda snap: getattr(snap, key), reverse=reverse)[:limit]


def parse_market_context(symbol: str, quote: QuoteV1) -> MarketContext | None:
    price = quote.last or quote.mark
    change_pct = quote.net_percent_change
    if price is None or change_pct is None:
        return None
    return MarketContext(symbol=symbol, price=price, change_pct=change_pct)


def rank_scan_results(
    snapshots: list[EquitySnapshot],
    *,
    settings: EquityScanSettings,
    movers_up: list[dict[str, Any]],
    movers_down: list[dict[str, Any]],
    market_context: list[MarketContext],
    scanned_symbols: int,
    generated_at: dt.datetime | None = None,
    rejected_symbols: dict[str, int] | None = None,
    bad_data: list[dict[str, Any]] | None = None,
    quote_coverage: dict[str, Any] | None = None,
) -> ScanResults:
    limits = settings.limits
    filters = settings.filters
    show_premarket = is_premarket_window(
        generated_at,
        start=settings.premarket_start_et,
    )
    show_movers = settings.include_movers and is_market_open(generated_at)

    prior_gainers = _top(
        snapshots,
        key="prior_day_pct",
        reverse=True,
        min_abs_pct=filters.prior_day_min_pct,
        limit=limits.prior_gainers,
    )
    prior_losers = _top(
        snapshots,
        key="prior_day_pct",
        reverse=False,
        min_abs_pct=filters.prior_day_min_pct,
        limit=limits.prior_losers,
    )

    premarket_gainers: list[EquitySnapshot] = []
    premarket_losers: list[EquitySnapshot] = []
    if show_premarket:
        premarket_gainers = _top(
            snapshots,
            key="session_gap_pct",
            reverse=True,
            min_abs_pct=filters.premarket_min_gap_pct,
            limit=limits.premarket_gainers,
        )
        premarket_losers = _top(
            snapshots,
            key="session_gap_pct",
            reverse=False,
            min_abs_pct=filters.premarket_min_gap_pct,
            limit=limits.premarket_losers,
        )
    filtered_movers_up: list[dict[str, Any]] = []
    filtered_movers_down: list[dict[str, Any]] = []
    if show_movers:
        filtered_movers_up, filtered_movers_down = filter_movers(
            movers_up,
            movers_down,
            min_abs_pct=settings.movers_min_abs_pct,
            limit=limits.movers_per_bucket,
        )

    return ScanResults(
        opening_focus=rank_opening_focus(snapshots, settings=settings),
        catalyst_watch=rank_catalyst_watch(snapshots, settings=settings),
        prior_gainers=prior_gainers,
        prior_losers=prior_losers,
        premarket_gainers=premarket_gainers,
        premarket_losers=premarket_losers,
        movers_up=filtered_movers_up,
        movers_down=filtered_movers_down,
        market_context=market_context,
        scanned_symbols=scanned_symbols,
        matched_symbols=len(snapshots),
        show_premarket=show_premarket,
        show_movers=show_movers,
        rejected_symbols=rejected_symbols,
        bad_data=bad_data,
        quote_coverage=quote_coverage,
    )
