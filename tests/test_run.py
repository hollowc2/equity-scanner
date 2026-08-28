"""Tests for run.py's orchestration entry point."""

from __future__ import annotations

import datetime as dt
import json

from schwab_gateway_sdk import QuoteV1

from equity_scanner import run
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.time_utils import EASTERN


async def test_run_scan_skips_market_holiday_before_gateway(monkeypatch):
    class AppSettingsShouldNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("AppSettings should not load on holidays")

    monkeypatch.setattr(
        run,
        "now_eastern",
        lambda: dt.datetime(2026, 7, 3, 8, 0, tzinfo=EASTERN),  # July 4th observed
    )
    monkeypatch.setattr(run, "AppSettings", AppSettingsShouldNotBeConstructed)

    messages = await run.run_scan(scan_config_path="missing.yaml")

    assert messages == []


async def test_run_scan_persists_phase_timings_with_fake_boundaries(monkeypatch, tmp_path):
    generated_at = dt.datetime(2026, 8, 26, 8, 0, tzinfo=EASTERN)
    now_utc = generated_at.astimezone(dt.timezone.utc)

    class FakeAppSettings:
        sec_user_agent = None
        alpha_vantage_api_key = None
        gateway_max_attempts = 1
        gateway_retry_backoff_seconds = 0.0
        discord_webhook_url = None

    class FakeGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeProvider:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_equity_quotes(self, symbols, *, batch_size=100):
            return {
                symbol: QuoteV1(
                    symbol=symbol,
                    event_timestamp=now_utc,
                    gateway_received_at=now_utc,
                    source="test",
                    session="extended" if symbol == "AAPL" else "regular",
                    last=105.0,
                    close=100.0,
                    net_percent_change=5.0,
                    volume=1_000_000,
                    stale=False,
                )
                for symbol in symbols
            }

        async def get_daily_bars(self, _symbol, days_back=None):
            return [
                {
                    "datetime": int(
                        dt.datetime(2026, 8, 24, 16, 0, tzinfo=dt.timezone.utc).timestamp()
                        * 1000
                    ),
                    "close": 100.0,
                    "volume": 1_000_000,
                },
                {
                    "datetime": int(
                        dt.datetime(2026, 8, 25, 16, 0, tzinfo=dt.timezone.utc).timestamp()
                        * 1000
                    ),
                    "close": 105.0,
                    "volume": 1_100_000,
                },
            ]

    settings = EquityScanSettings(
        universes=["custom"],
        universe_dir=str(tmp_path),
        custom_watchlist=str(tmp_path / "custom.txt"),
        report_dir=str(tmp_path / "reports"),
        context_symbols=["SPY"],
        filters={"min_rvol": 0.0},
        news={"enabled": False},
    )
    monkeypatch.setattr(run, "now_eastern", lambda: generated_at)
    monkeypatch.setattr(run, "AppSettings", FakeAppSettings)
    monkeypatch.setattr(run, "load_equity_scan_config", lambda _path: settings)
    monkeypatch.setattr(run, "load_universes", lambda *_args, **_kwargs: {"custom": ["AAPL"]})
    monkeypatch.setattr(run, "build_gateway_client", lambda _settings: FakeGateway())
    monkeypatch.setattr(run, "GatewayEquityDataProvider", FakeProvider)

    messages = await run.run_scan(scan_config_path="unused.yaml", dry_run=True)

    payload = json.loads((tmp_path / "reports" / "2026-08-26.json").read_text())
    timings = payload["phase_timings_ms"]
    assert messages
    assert {
        "setup",
        "quotes",
        "context_quotes",
        "rvol_history",
        "preliminary_ranking",
        "prior_day_history",
        "snapshot_finalize",
        "news",
        "movers",
        "final_ranking",
        "report",
        "markdown_archive",
        "total_generation",
    } <= timings.keys()
    assert all(value >= 0 for value in timings.values())
