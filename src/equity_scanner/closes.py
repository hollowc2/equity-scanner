"""Prior-day change from the official closes each morning scan already receives.

Before the open, Schwab's `netPercentChange` already includes premarket trades, so it
is today's move, not yesterday's. But `quote.close` is still the previous session's
official close. Keeping each pre-open scan's closes lets the next trading day's scan
compute yesterday's close-to-close change for the whole universe without one history
request per symbol.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import tempfile
from pathlib import Path

from schwab_gateway_sdk import QuoteV1

from equity_scanner.time_utils import EASTERN, MARKET_OPEN, is_trading_day

log = logging.getLogger(__name__)


def previous_trading_day(day: dt.date) -> dt.date:
    previous = day - dt.timedelta(days=1)
    while not is_trading_day(previous):
        previous -= dt.timedelta(days=1)
    return previous


def _closes_path(closes_dir: str | Path, day: dt.date) -> Path:
    return Path(closes_dir) / f"{day.isoformat()}.json"


def record_closes(
    quotes: dict[str, QuoteV1],
    *,
    closes_dir: str | Path,
    generated_at: dt.datetime,
) -> Path | None:
    """Save this morning's previous-session closes, keyed by the session they close.

    Only pre-open captures on a trading day are kept: after the open the quote's
    close is still yesterday's, but a later run must never overwrite a morning
    capture with data whose meaning may have shifted.
    """
    now = generated_at.astimezone(EASTERN)
    if not is_trading_day(now.date()) or now.time() >= MARKET_OPEN:
        return None
    closes = {
        symbol: float(quote.close)
        for symbol, quote in sorted(quotes.items())
        if quote.close is not None and quote.close > 0
    }
    if not closes:
        return None
    # The file for trading day D holds the closes of the session before D.
    path = _closes_path(closes_dir, now.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": now.date().isoformat(),
        "session_closed": previous_trading_day(now.date()).isoformat(),
        "captured_at": now.isoformat(),
        "closes": closes,
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(handle.name, path)
    return path


def load_previous_closes(closes_dir: str | Path, *, today: dt.date) -> dict[str, float]:
    """Closes captured on the previous trading day, i.e. two sessions ago."""
    path = _closes_path(closes_dir, previous_trading_day(today))
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("prior_day_closes_unreadable path=%s error=%s", path, exc)
        return {}
    closes = payload.get("closes") if isinstance(payload, dict) else None
    if not isinstance(closes, dict):
        return {}
    return {
        str(symbol): float(close)
        for symbol, close in closes.items()
        if isinstance(close, (int, float)) and close > 0
    }


def prior_day_changes_from_closes(
    quotes: dict[str, QuoteV1],
    previous_closes: dict[str, float],
) -> dict[str, float]:
    """Yesterday's close-to-close percent change per symbol present in both."""
    changes: dict[str, float] = {}
    for symbol, quote in quotes.items():
        before = previous_closes.get(symbol)
        if before is None or quote.close is None or quote.close <= 0:
            continue
        changes[symbol] = (quote.close - before) / before * 100.0
    return changes
