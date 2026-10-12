"""Relative-volume helpers, ported from ButterflyGuy's equity_scan/volume.py to run
against a GatewayEquityDataProvider. Completed sessions are selected using the
scan's Eastern session date; missing history is logged without exception details."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Protocol

from schwab_gateway_sdk import QuoteV1

from equity_scanner.time_utils import EASTERN, now_eastern

log = logging.getLogger(__name__)


def _session_start_ms(session_date: dt.date | None) -> int:
    day = session_date or now_eastern().date()
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=EASTERN).timestamp() * 1000)


class DailyBarsProvider(Protocol):
    async def get_daily_bars(self, symbol: str, days_back: int | None = None) -> list[dict]: ...


def _as_int(value: object) -> int:
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def avg_daily_volume(
    candles: list[dict], *, lookback: int = 20, session_date: dt.date | None = None
) -> float | None:
    """Average daily volume from completed sessions (excludes today)."""
    if not candles or lookback <= 0:
        return None

    today_start_ms = _session_start_ms(session_date)
    volumes = [
        _as_int(candle.get("volume"))
        for candle in candles
        if _as_int(candle.get("volume")) > 0
        and _as_int(candle.get("datetime")) < today_start_ms
    ]
    if not volumes:
        return None

    recent = volumes[-lookback:]
    if len(recent) < max(1, lookback // 2):
        return None
    return sum(recent) / len(recent)


def prior_session_pct_change(
    candles: list[dict], *, session_date: dt.date | None = None
) -> float | None:
    """Close-to-close percent change for the last completed daily session."""
    if not candles:
        return None

    today_start_ms = _session_start_ms(session_date)
    completed = [
        candle
        for candle in candles
        if _as_int(candle.get("datetime")) < today_start_ms
    ]
    if len(completed) < 2:
        return None

    prev_close = completed[-2].get("close")
    last_close = completed[-1].get("close")
    try:
        prev = float(prev_close)
        last = float(last_close)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    return (last - prev) / prev * 100.0


def compute_rvol(premarket_volume: int, avg_volume: float | None) -> float | None:
    if avg_volume is None or avg_volume <= 0 or premarket_volume <= 0:
        return None
    return premarket_volume / avg_volume


def todays_premarket_volume(
    quote: QuoteV1,
    *,
    generated_at: dt.datetime,
    premarket_start: str = "04:00",
) -> int:
    """The quote's volume when it reflects trading today since the premarket start.

    Before the open Schwab stamps its regular-session block with premarket trades, so
    the gateway (which keeps the regular session on a timestamp tie) reports
    session="regular" with today's premarket cumulative volume. The event timestamp,
    not the session label, says whether the volume is today's: a quote last updated
    yesterday carries after-hours activity, not premarket."""
    volume = _as_int(quote.volume)
    if volume <= 0 or quote.event_timestamp is None:
        return 0
    event = quote.event_timestamp.astimezone(EASTERN)
    now = generated_at.astimezone(EASTERN)
    if event.date() != now.date() or event.time() < dt.time.fromisoformat(premarket_start):
        return 0
    return volume


def symbols_needing_rvol_fetch(
    quotes: dict[str, QuoteV1],
    *,
    in_premarket: bool,
    generated_at: dt.datetime,
    premarket_start: str = "04:00",
) -> list[str]:
    """Symbols with premarket volume today — only these need avg-volume for RVOL.

    Gated on `in_premarket`: rvol is specifically a premarket-activity signal, so an
    after-hours run must not treat post-close activity as premarket."""
    if not in_premarket:
        return []
    return sorted(
        symbol
        for symbol, quote in quotes.items()
        if todays_premarket_volume(
            quote, generated_at=generated_at, premarket_start=premarket_start
        )
        > 0
    )


async def fetch_avg_volumes(
    provider: DailyBarsProvider,
    symbols: list[str],
    *,
    lookback_days: int = 20,
    concurrency: int = 10,
    session_date: dt.date | None = None,
) -> dict[str, float]:
    """Fetch 20-day average daily volume for each symbol."""
    if not symbols:
        return {}

    sem = asyncio.Semaphore(concurrency)
    results: dict[str, float] = {}
    session_date = session_date or now_eastern().date()

    async def _fetch_one(symbol: str) -> None:
        async with sem:
            try:
                candles = await provider.get_daily_bars(symbol, days_back=lookback_days)
                avg = avg_daily_volume(
                    candles, lookback=lookback_days, session_date=session_date
                )
                if avg is not None:
                    results[symbol] = avg
                else:
                    log.warning(
                        "equity_scan_avg_volume_unavailable symbol=%s "
                        "reason=insufficient_completed_volume_bars bars=%d lookback=%d",
                        symbol, len(candles), lookback_days,
                    )
            except Exception as exc:
                log.warning(
                    "equity_scan_avg_volume_fetch_failed symbol=%s error_type=%s",
                    symbol, type(exc).__name__,
                )
                return

    await asyncio.gather(*(_fetch_one(symbol) for symbol in symbols))
    return results


async def fetch_prior_day_changes(
    provider: DailyBarsProvider,
    symbols: list[str],
    *,
    concurrency: int = 10,
    days_back: int | None = 20,
    session_date: dt.date | None = None,
) -> dict[str, float]:
    """Fetch true prior-session close-to-close percent changes."""
    if not symbols:
        return {}

    sem = asyncio.Semaphore(concurrency)
    results: dict[str, float] = {}
    session_date = session_date or now_eastern().date()

    async def _fetch_one(symbol: str) -> None:
        async with sem:
            try:
                candles = await provider.get_daily_bars(symbol, days_back=days_back)
                pct = prior_session_pct_change(candles, session_date=session_date)
                if pct is not None:
                    results[symbol] = pct
                else:
                    log.warning(
                        "equity_scan_prior_day_unavailable symbol=%s "
                        "reason=insufficient_valid_completed_closes bars=%d",
                        symbol, len(candles),
                    )
            except Exception as exc:
                log.warning(
                    "equity_scan_prior_day_fetch_failed symbol=%s error_type=%s",
                    symbol, type(exc).__name__,
                )
                return

    await asyncio.gather(*(_fetch_one(symbol) for symbol in symbols))
    return results
