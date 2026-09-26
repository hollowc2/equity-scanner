"""Fixture parity against the preserved ButterflyGuy reference implementation.

ButterflyGuy is imported only by this test from the explicitly located source checkout;
it is not a package dependency and is never imported by standalone runtime code.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
from pathlib import Path

import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import build_snapshots, rank_scan_results

SOURCE_ROOT = Path("/mnt/Repos/Trading/Butterflyguy/src")
# ButterflyGuy removed its embedded scanner after cutover (79bbe50), so the checkout
# alone is not enough; the reference package must still be present.
REFERENCE_AVAILABLE = (SOURCE_ROOT / "butterfly_guy/equity_scan/__init__.py").exists()
FIXTURE = Path(__file__).parent / "fixtures/parity_quotes.json"


@pytest.mark.skipif(not REFERENCE_AVAILABLE, reason="ButterflyGuy reference scanner unavailable")
def test_filter_calculation_and_ranking_parity() -> None:
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        reference_config = importlib.import_module("butterfly_guy.equity_scan.config")
        reference_scanner = importlib.import_module("butterfly_guy.equity_scan.scanner")
    finally:
        sys.path.remove(str(SOURCE_ROOT))

    rows = json.loads(FIXTURE.read_text())
    now = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.timezone.utc)
    symbol_map = {row["symbol"]: {"sp500"} for row in rows}
    gateway_quotes = {
        row["symbol"]: QuoteV1(
            symbol=row["symbol"], event_timestamp=now, gateway_received_at=now,
            source="fixture", session=row["session"], last=row["last"],
            close=row["close"], net_percent_change=row["net_pct"], volume=row["volume"],
            stale=False,
        )
        for row in rows
    }
    raw_quotes = {
        row["symbol"]: {"quote": {"closePrice": row["close"], "lastPrice": row["last"],
                                   "netPercentChange": row["net_pct"],
                                   "totalVolume": row["volume"]}}
        for row in rows
    }
    settings = EquityScanSettings()
    reference_settings = reference_config.EquityScanSettings()
    standalone_rejected: dict[str, int] = {}
    reference_rejected: dict[str, int] = {}
    standalone = build_snapshots(
        gateway_quotes, symbol_map, settings, rejected_symbols=standalone_rejected
    )
    reference = reference_scanner.build_snapshots(
        raw_quotes, symbol_map, reference_settings, rejected_symbols=reference_rejected
    )

    def values(snapshots):
        return [
            (s.symbol, s.price, s.prior_day_pct, s.session_gap_pct, s.volume)
            for s in snapshots
        ]

    assert values(standalone) == values(reference)
    assert standalone_rejected == reference_rejected
    standalone_result = rank_scan_results(
        standalone, settings=settings, movers_up=[], movers_down=[], market_context=[],
        scanned_symbols=len(rows), generated_at=now,
    )
    reference_result = reference_scanner.rank_scan_results(
        reference, settings=reference_settings, movers_up=[], movers_down=[], market_context=[],
        scanned_symbols=len(rows), generated_at=now,
    )
    for field in ("prior_gainers", "prior_losers", "premarket_gainers", "premarket_losers"):
        assert [s.symbol for s in getattr(standalone_result, field)] == [
            s.symbol for s in getattr(reference_result, field)
        ]
    assert [item.reasons for item in standalone_result.opening_focus] == [
        item.reasons for item in reference_result.opening_focus
    ]
