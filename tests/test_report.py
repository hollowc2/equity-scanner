"""Tests for Discord report formatting/archiving, ported from ButterflyGuy's
equity_scan report tests, adapted to build EquitySnapshot via the flat QuoteV1
shape instead of the old two-payload dict."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict

from schwab_gateway_sdk import QuoteV1

from equity_scanner.news import NewsImpact
from equity_scanner.provider import QuoteBatchFailure, QuoteCoverage
from equity_scanner.report import (
    _format_market_context,
    archive_report,
    archive_report_json,
    build_report,
)
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import (
    MarketContext,
    attach_news_impacts,
    parse_equity_quote,
    rank_scan_results,
)
from equity_scanner.time_utils import EASTERN


def _premarket_et() -> dt.datetime:
    return dt.datetime(2026, 6, 8, 8, 0, tzinfo=EASTERN)


def _quote(
    *,
    close: float,
    last: float,
    net_pct: float,
    volume: int,
    session: str = "regular",
) -> QuoteV1:
    return QuoteV1(
        symbol="TEST",
        gateway_received_at=dt.datetime.now(dt.timezone.utc),
        source="test",
        session=session,
        close=close,
        last=last,
        net_percent_change=net_pct,
        volume=volume,
        stale=False,
    )


def test_archive_report_writes_dated_markdown(tmp_path):
    generated_at = _premarket_et()
    path = archive_report(
        ["line one", "line two"],
        report_dir=str(tmp_path),
        generated_at=generated_at,
    )
    assert path.name == "2026-06-08.md"
    assert "line one" in path.read_text()


def test_archive_report_json_writes_scan_internals(tmp_path):
    settings = EquityScanSettings()
    snap = parse_equity_quote(
        "WIN",
        _quote(close=100, last=110, net_pct=10.0, volume=1_000_000),
        universes={"sp500"},
    )
    assert snap is not None
    generated_at = _premarket_et()
    results = rank_scan_results(
        [snap],
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=1,
        generated_at=generated_at,
        rejected_symbols={"reference_price_deviation": 1},
        bad_data=[{"symbol": "BAD", "reason": "reference_price_deviation"}],
    )
    path = archive_report_json(results, report_dir=str(tmp_path), generated_at=generated_at)
    text = path.read_text()
    assert path.name == "2026-06-08.json"
    assert '"opening_focus"' in text
    assert '"reference_price_deviation": 1' in text


def test_archive_report_json_serializes_incomplete_quote_coverage(tmp_path):
    settings = EquityScanSettings()
    generated_at = _premarket_et()
    coverage = QuoteCoverage(
        mode="parity-bounded-recovery",
        requested_count=2,
        returned_count=1,
        stale_retained_count=1,
        unavailable_count=1,
        failed_batch_count=1,
        requested_symbols=("AAPL", "MSFT"),
        returned_symbols=("AAPL",),
        stale_retained_symbols=("AAPL",),
        unavailable_symbols=("MSFT",),
        failed_batches=(
            QuoteBatchFailure(
                batch_index=1,
                symbols=("MSFT",),
                error_type="GatewayTimeoutError",
                error_message="gateway quote upstream timed out",
                recovery_attempted=True,
            ),
        ),
        initial_call_count=2,
        recovery_call_count=1,
        max_concurrency=2,
        complete=False,
        verdict="incomplete",
    )
    results = rank_scan_results(
        [],
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=1,
        generated_at=generated_at,
        quote_coverage=asdict(coverage),
    )

    path = archive_report_json(results, report_dir=str(tmp_path), generated_at=generated_at)
    payload = json.loads(path.read_text())

    assert payload["scanned_symbols"] == 1
    assert payload["quote_coverage"]["requested_count"] == 2
    assert payload["quote_coverage"]["returned_count"] == 1
    assert payload["quote_coverage"]["stale_retained_symbols"] == ["AAPL"]
    assert payload["quote_coverage"]["unavailable_symbols"] == ["MSFT"]
    assert payload["quote_coverage"]["failed_batches"][0]["error_type"] == (
        "GatewayTimeoutError"
    )


def test_build_report_omits_routine_filter_failures_and_quote_source_noise():
    settings = EquityScanSettings()
    snap = parse_equity_quote(
        "WIN",
        _quote(close=100, last=110, net_pct=10.0, volume=1_000_000),
        universes={"sp500"},
    )
    assert snap is not None
    results = rank_scan_results(
        [snap],
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=2,
        generated_at=_premarket_et(),
        rejected_symbols={"filter_failed": 1},
        bad_data=[],
    )
    report = "\n".join(build_report(results, settings=settings, generated_at=_premarket_et()))
    assert "Quote Sanity" not in report
    assert "filter_failed" not in report


def test_build_report_groups_snapshots_by_sector():
    settings = EquityScanSettings()
    settings.group_by_sector = True
    snapshots = [
        parse_equity_quote(
            "WIN",
            _quote(close=100, last=110, net_pct=10.0, volume=1_000_000),
            universes={"sp500"},
            sector="Information Technology",
        ),
        parse_equity_quote(
            "LOSE",
            _quote(close=100, last=90, net_pct=-10.0, volume=1_000_000),
            universes={"sp500"},
            sector="Health Care",
        ),
    ]
    snapshots = [snap for snap in snapshots if snap is not None]
    results = rank_scan_results(
        snapshots,
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=2,
        generated_at=_premarket_et(),
    )
    messages = build_report(results, settings=settings)
    report = "\n".join(messages)
    assert "Opening Focus" in report
    assert "▸ __**TECH**__ · 1" in report
    assert "▸ __**HEALTH**__ · 1" in report
    assert "🟢 **WIN** **+10.0%**" in report
    assert "🔴 **LOSE** **-10.0%**" in report


def test_build_report_includes_catalyst_watch():
    settings = EquityScanSettings()
    snap = parse_equity_quote(
        "CAT",
        _quote(close=100, last=104, net_pct=4.0, volume=1_000_000),
        universes={"sp500"},
    )
    assert snap is not None
    snapshots = attach_news_impacts(
        [snap],
        {
            "CAT": NewsImpact(
                symbol="CAT",
                score=5.0,
                reasons=("upcoming earnings",),
                upcoming_events=("earnings expected 2026-06-10",),
                providers=("alpha_vantage",),
            )
        },
    )
    results = rank_scan_results(
        snapshots,
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=1,
        generated_at=_premarket_et(),
    )
    report = "\n".join(build_report(results, settings=settings, generated_at=_premarket_et()))
    assert "Catalyst Watch" in report
    assert "**CAT** news 5.0" in report
    assert "earnings expected 2026-06-10" in report


def test_build_report_splits_long_output():
    settings = EquityScanSettings()
    settings.limits.prior_gainers = 30
    snapshots = []
    for i in range(30):
        snap = parse_equity_quote(
            f"T{i:02d}",
            _quote(close=100, last=110 + i, net_pct=10 + i, volume=1_000_000),
            universes={"sp500"},
        )
        assert snap is not None
        snapshots.append(snap)
    results = rank_scan_results(
        snapshots,
        settings=settings,
        movers_up=[],
        movers_down=[],
        market_context=[],
        scanned_symbols=30,
        generated_at=_premarket_et(),
    )
    messages = build_report(results, settings=settings)
    assert len(messages) >= 1
    assert all(len(message) <= 2000 for message in messages)
    assert "Morning Equity Scan" in messages[0]


def test_format_market_context_orders_priority_symbols_deterministically():
    """Regression test: the priority lookup used to be a set, so this ordering
    depended on Python's per-process string hash randomization."""
    context = [
        MarketContext(symbol="$DJI", price=1.0, change_pct=0.1),
        MarketContext(symbol="QQQ", price=1.0, change_pct=0.2),
        MarketContext(symbol="$SPX", price=1.0, change_pct=0.3),
        MarketContext(symbol="$COMPX", price=1.0, change_pct=0.4),
        MarketContext(symbol="SPY", price=1.0, change_pct=0.5),
        MarketContext(symbol="AAPL", price=1.0, change_pct=0.6),
    ]

    tape = _format_market_context(context)

    assert tape == (
        "**$SPX** +0.3% · **SPY** +0.5% · **QQQ** +0.2% · **$COMPX** +0.4% · "
        "**$DJI** +0.1% · **AAPL** +0.6%"
    )
