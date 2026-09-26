"""Deterministic parity for provider-neutral behavior retained from ButterflyGuy."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from equity_scanner import news as candidate_news
from equity_scanner import report as candidate_report
from equity_scanner import scanner as candidate_scanner
from equity_scanner import universes as candidate_universes
from equity_scanner import volume as candidate_volume
from equity_scanner.scan_config import EquityScanSettings as CandidateSettings

SOURCE_ROOT = Path("/mnt/Repos/Trading/Butterflyguy/src")
# ButterflyGuy removed its embedded scanner after cutover (79bbe50), so the checkout
# alone is not enough; the reference package must still be present.
REFERENCE_AVAILABLE = (SOURCE_ROOT / "butterfly_guy/equity_scan/__init__.py").exists()
pytestmark = pytest.mark.skipif(
    not REFERENCE_AVAILABLE, reason="ButterflyGuy reference scanner unavailable"
)


def _reference_modules():
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        return {
            name: importlib.import_module(f"butterfly_guy.equity_scan.{name}")
            for name in ("config", "news", "report", "scanner", "universes", "volume")
        }
    finally:
        sys.path.remove(str(SOURCE_ROOT))


def test_universe_loading_and_volume_calculations_match_reference(tmp_path):
    reference = _reference_modules()
    universe_dir = tmp_path / "universes"
    universe_dir.mkdir()
    (universe_dir / "sp500.txt").write_text("aapl\nMSFT # note\n")
    custom = tmp_path / "custom.txt"
    custom.write_text("tsla\nAAPL\n")

    candidate_loaded = candidate_universes.load_universes(
        ["sp500", "custom"], universe_dir=universe_dir, custom_watchlist=custom
    )
    reference_loaded = reference["universes"].load_universes(
        ["sp500", "custom"], universe_dir=universe_dir, custom_watchlist=custom
    )
    assert candidate_loaded == reference_loaded
    assert candidate_universes.build_symbol_map(candidate_loaded) == reference[
        "universes"
    ].build_symbol_map(reference_loaded)

    bars = [
        {
            "datetime": int(
                dt.datetime(2026, 9, day, 20, tzinfo=dt.timezone.utc).timestamp() * 1000
            ),
            "close": 100.0 + day,
            "volume": day * 100_000,
        }
        for day in range(1, 8)
    ]
    assert candidate_volume.avg_daily_volume(bars, lookback=5) == reference[
        "volume"
    ].avg_daily_volume(bars, lookback=5)
    assert candidate_volume.prior_session_pct_change(bars) == reference[
        "volume"
    ].prior_session_pct_change(bars)
    assert candidate_volume.compute_rvol(250_000, 1_000_000) == reference[
        "volume"
    ].compute_rvol(250_000, 1_000_000)


def test_news_ranking_movers_report_chunking_and_archives_match_reference(tmp_path):
    reference = _reference_modules()
    generated_at = dt.datetime(2026, 9, 10, 8, 0, tzinfo=dt.timezone.utc)
    candidate_settings = CandidateSettings(group_by_sector=False, include_movers=True)
    reference_settings = reference["config"].EquityScanSettings(
        group_by_sector=False, include_movers=True
    )
    candidate_settings.limits.prior_gainers = 30
    reference_settings.limits.prior_gainers = 30

    filing_payload = {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "filingDate": ["2026-09-10"],
                "primaryDocDescription": ["Current report"],
            }
        }
    }
    candidate_impact = candidate_news._recent_sec_filings(
        "S00", filing_payload, today=generated_at.date(), settings=candidate_settings.news
    )
    reference_impact = reference["news"]._recent_sec_filings(
        "S00", filing_payload, today=generated_at.date(), settings=reference_settings.news
    )
    assert asdict(candidate_impact) == asdict(reference_impact)

    candidate_snapshots = []
    reference_snapshots = []
    for index in range(30):
        values = {
            "symbol": f"S{index:02d}",
            "price": 110.0 + index,
            "prior_close": 100.0,
            "prior_day_pct": 10.0 + index,
            "session_gap_pct": 10.0 + index,
            "volume": 1_000_000 + index,
            "premarket_volume": 100_000 + index,
            "avg_volume_20d": 1_000_000.0,
            "rvol": 0.1,
            "sector": "Technology",
            "universes": ("sp500",),
        }
        candidate_snapshots.append(candidate_scanner.EquitySnapshot(**values))
        reference_snapshots.append(reference["scanner"].EquitySnapshot(**values))

    candidate_snapshots = candidate_scanner.attach_news_impacts(
        candidate_snapshots, {"S00": candidate_impact}
    )
    reference_snapshots = reference["scanner"].attach_news_impacts(
        reference_snapshots, {"S00": reference_impact}
    )
    movers_up = [
        {"symbol": "UP", "changePercent": 5.0},
        {"symbol": "SMALL", "changePercent": 0.5},
    ]
    movers_down = [{"symbol": "DOWN", "changePercent": -4.0}]

    candidate_results = candidate_scanner.rank_scan_results(
        candidate_snapshots,
        settings=candidate_settings,
        movers_up=movers_up,
        movers_down=movers_down,
        market_context=[],
        scanned_symbols=30,
        generated_at=generated_at,
    )
    reference_results = reference["scanner"].rank_scan_results(
        reference_snapshots,
        settings=reference_settings,
        movers_up=movers_up,
        movers_down=movers_down,
        market_context=[],
        scanned_symbols=30,
        generated_at=generated_at,
    )
    assert [item.snapshot.symbol for item in candidate_results.opening_focus] == [
        item.snapshot.symbol for item in reference_results.opening_focus
    ]
    assert [item.symbol for item in candidate_results.catalyst_watch] == [
        item.symbol for item in reference_results.catalyst_watch
    ]
    assert candidate_results.movers_up == reference_results.movers_up
    assert candidate_results.movers_down == reference_results.movers_down

    candidate_messages = candidate_report.build_report(
        candidate_results, settings=candidate_settings, generated_at=generated_at
    )
    reference_messages = reference["report"].build_report(
        reference_results, settings=reference_settings, generated_at=generated_at
    )
    assert candidate_messages == reference_messages
    assert len(candidate_messages) > 1
    assert all(len(message) <= 1900 for message in candidate_messages)

    candidate_markdown = candidate_report.archive_report(
        candidate_messages,
        report_dir=str(tmp_path / "candidate"),
        generated_at=generated_at,
    )
    reference_markdown = reference["report"].archive_report(
        reference_messages,
        report_dir=str(tmp_path / "reference"),
        generated_at=generated_at,
    )
    assert candidate_markdown.name == reference_markdown.name == "2026-09-10.md"
    assert candidate_markdown.read_text() == reference_markdown.read_text()

    candidate_json = candidate_report.archive_report_json(
        candidate_results,
        report_dir=str(tmp_path / "candidate"),
        generated_at=generated_at,
    )
    reference_json = reference["report"].archive_report_json(
        reference_results,
        report_dir=str(tmp_path / "reference"),
        generated_at=generated_at,
    )
    candidate_payload = json.loads(candidate_json.read_text())
    reference_payload = json.loads(reference_json.read_text())
    assert candidate_payload.pop("phase_timings_ms") is None
    assert candidate_payload.pop("quote_coverage") is None
    assert candidate_payload == reference_payload
