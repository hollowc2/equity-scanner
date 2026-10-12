"""Tests for Discord report formatting/archiving, ported from ButterflyGuy's
equity_scan report tests, adapted to build EquitySnapshot via the flat QuoteV1
shape instead of the old two-payload dict."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict

import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner.news import NewsImpact
from equity_scanner.provider import QuoteBatchFailure, QuoteCoverage
from equity_scanner.report import (
    _fmt_rvol,
    _format_market_context,
    archive_report,
    archive_report_json,
    build_report,
)
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import (
    EquitySnapshot,
    MarketContext,
    OpeningFocusItem,
    ScanResults,
    attach_news_impacts,
    parse_equity_quote,
    rank_scan_results,
)
from equity_scanner.time_utils import EASTERN


@pytest.mark.parametrize(
    ("rvol", "expected"),
    [(0.03, "0.03x"), (0.001, "<0.01x"), (0.0, "0.00x"), (0.7, "0.7x"), (1.2, "🔥RVOL 1.2x")],
)
def test_rvol_format_preserves_small_positive_volume(rvol, expected):
    snapshot = EquitySnapshot(
        symbol="TEST", price=100, prior_close=100, prior_day_pct=0,
        session_gap_pct=0, volume=3000, premarket_volume=3000,
        avg_volume_20d=100000, rvol=rvol, sector="Unknown", universes=(),
    )
    assert expected in _fmt_rvol(snapshot)


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
            ),
        ),
        initial_call_count=2,
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


def test_archives_are_replaced_atomically(tmp_path, monkeypatch):
    from equity_scanner import files

    generated_at = _premarket_et()
    path = archive_report(["old"], report_dir=str(tmp_path), generated_at=generated_at)

    def fail_replace(_source, _destination):
        raise OSError("simulated crash")

    monkeypatch.setattr(files.os, "replace", fail_replace)
    with pytest.raises(OSError):
        archive_report(["new"], report_dir=str(tmp_path), generated_at=generated_at)

    assert path.read_text() == "old\n"
    assert list(tmp_path.iterdir()) == [path]


def _snapshot(symbol: str, **overrides) -> EquitySnapshot:
    fields = dict(
        symbol=symbol, price=50.0, prior_close=48.0, prior_day_pct=-6.0,
        session_gap_pct=4.0, volume=900_000, premarket_volume=900_000,
        avg_volume_20d=3_000_000, rvol=0.3, sector="NASDAQ", universes=("liquid",),
    )
    fields.update(overrides)
    return EquitySnapshot(**fields)


def _results(**overrides) -> ScanResults:
    fields = dict(
        opening_focus=[], catalyst_watch=[], prior_gainers=[], prior_losers=[],
        premarket_gainers=[], premarket_losers=[], movers_up=[], movers_down=[],
        market_context=[], scanned_symbols=10, matched_symbols=10,
    )
    fields.update(overrides)
    return ScanResults(**fields)


def test_build_report_leads_with_top_setups_and_stars_them_in_lists():
    settings = EquityScanSettings()
    top = _snapshot("TOP")
    rest = _snapshot("REST", session_gap_pct=-2.5)
    results = _results(
        opening_focus=[
            OpeningFocusItem(top, score=20.0, reasons=("gap with volume",), headline=True),
            OpeningFocusItem(rest, score=9.0, reasons=("gap with volume",)),
        ],
        prior_losers=[top],
        premarket_losers=[rest],
    )

    report = "\n".join(build_report(results, settings=settings, generated_at=_premarket_et()))

    assert report.index("Top Setups") < report.index("Opening Focus")
    assert "🟢 **TOP  +4.0%** gap @ $50.00 · 900K pm vol · RVOL 0.3x" in report
    assert "-# ↳ gap with volume · prior -6.0%" in report
    assert "**🎯 Opening Focus** (1)\n🔴 **REST** gap -2.5%" in report
    assert "**TOP** **-6.0%** ⭐" in report
    assert "**REST** **-2.5%** @" in report


def test_build_report_omits_top_setups_without_headlines():
    settings = EquityScanSettings()
    item = OpeningFocusItem(_snapshot("ONLY"), score=5.0, reasons=("sector cluster",))

    results = _results(opening_focus=[item])

    report = "\n".join(build_report(results, settings=settings, generated_at=_premarket_et()))

    assert "Top Setups" not in report
    assert "**🎯 Opening Focus** (1)\n🟢 **ONLY**" in report


def test_build_report_quiets_routine_tags():
    settings = EquityScanSettings()
    settings.group_by_sector = True
    stale = ("stale", "gateway_stale", "prior_day_unavailable")
    results = _results(
        prior_gainers=[_snapshot("OLD", prior_day_pct=5.0, data_quality_flags=stale)],
        premarket_gainers=[
            _snapshot("GAP", universes=("liquid", "sp500"), data_quality_flags=stale)
        ],
    )

    report = "\n".join(build_report(results, settings=settings, generated_at=_premarket_et()))

    # Only exchange-fallback sectors: no group header.
    assert "OTHER" not in report
    assert "Liq" not in report.split("\n", 3)[3]
    assert (
        "**GAP** **+4.0%** @ $50.00 · 900K vol · RVOL 0.3x · S&P · "
        "⚠ stale,prior_day_unavailable"
    ) in report
    # Staleness is expected on yesterday's movers; other flags still show.
    assert "**OLD** **+5.0%** @ $50.00 · 900K vol · RVOL 0.3x · ⚠ prior_day_unavailable" in report
