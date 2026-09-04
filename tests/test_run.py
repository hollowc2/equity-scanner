"""Tests for run.py's orchestration entry point."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner import run
from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import QuoteCollection, QuoteCoverage
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


@pytest.mark.parametrize(
    "mode", ("parity-bounded-recovery", "parity-paced-recovery")
)
async def test_parity_quote_recovery_mode_requires_dry_run(mode):
    with pytest.raises(ValueError, match="requires --dry-run"):
        await run.run_scan(quote_coverage_mode=mode)


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

        async def get_equity_quote_collection(
            self,
            symbols,
            *,
            batch_size=100,
            concurrency=4,
            mode="strict",
            max_recovery_attempts=1,
        ):
            quotes = await self.get_equity_quotes(symbols, batch_size=batch_size)
            ordered = tuple(dict.fromkeys(symbols))
            return QuoteCollection(
                quotes=quotes,
                coverage=QuoteCoverage(
                    mode=mode,
                    requested_count=len(ordered),
                    returned_count=len(ordered),
                    stale_retained_count=0,
                    unavailable_count=0,
                    failed_batch_count=0,
                    requested_symbols=ordered,
                    returned_symbols=ordered,
                    stale_retained_symbols=(),
                    unavailable_symbols=(),
                    failed_batches=(),
                    initial_call_count=1,
                    recovery_call_count=0,
                    max_concurrency=1,
                    complete=True,
                    verdict="complete",
                ),
            )

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


async def test_mocked_twenty_batch_paced_parity_recovers_three_504s(
    monkeypatch, tmp_path
):
    generated_at = dt.datetime(2026, 9, 1, 8, 0, tzinfo=EASTERN)
    now_utc = generated_at.astimezone(dt.timezone.utc)
    attempts: dict[str, int] = {}

    async def handler(request):
        assert request.url.path == "/v1/quotes"
        symbols = request.url.params["symbols"].split(",")
        batch = symbols[0]
        attempts[batch] = attempts.get(batch, 0) + 1
        if batch in {"S0300", "S0900", "S1500"} and attempts[batch] == 1:
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "quotes": [
                    {
                        "symbol": symbol,
                        "event_timestamp": now_utc.isoformat(),
                        "gateway_received_at": now_utc.isoformat(),
                        "source": "mock-proof",
                        "session": "regular",
                        "last": 10.0,
                        "close": 9.0,
                        "net_percent_change": 11.1,
                        "volume": 1_000_000,
                        "stale": False,
                    }
                    for symbol in symbols
                ],
            },
        )

    http = httpx.AsyncClient(
        base_url="https://gateway.internal",
        transport=httpx.MockTransport(handler),
    )
    gateway = build_gateway_client(
        AppSettings.model_validate(
            {
                "SCHWAB_GATEWAY_URL": "https://gateway.internal",
                "SCHWAB_GATEWAY_API_KEY": "test-only",
            }
        ),
        client=http,
    )

    class FakeAppSettings:
        sec_user_agent = None
        alpha_vantage_api_key = None
        gateway_max_attempts = 1
        gateway_retry_backoff_seconds = 0.0
        discord_webhook_url = None

    settings = EquityScanSettings(
        universes=["custom"],
        universe_dir=str(tmp_path),
        custom_watchlist=str(tmp_path / "custom.txt"),
        report_dir=str(tmp_path / "reports"),
        context_symbols=[],
        filters={
            "min_rvol": 0.0,
            "prior_day_min_pct": 100.0,
            "premarket_min_gap_pct": 100.0,
        },
        news={"enabled": False},
    )
    symbols = [f"S{i:04d}" for i in range(2000)]
    monkeypatch.setattr(run, "now_eastern", lambda: generated_at)
    monkeypatch.setattr(run, "AppSettings", FakeAppSettings)
    monkeypatch.setattr(run, "load_equity_scan_config", lambda _path: settings)
    monkeypatch.setattr(run, "load_universes", lambda *_args, **_kwargs: {"custom": symbols})
    monkeypatch.setattr(run, "build_gateway_client", lambda _settings: gateway)

    try:
        await run.run_scan(
            scan_config_path="unused.yaml",
            dry_run=True,
            quote_coverage_mode="parity-paced-recovery",
        )
    finally:
        await http.aclose()

    payload = json.loads((tmp_path / "reports" / "2026-09-01.json").read_text())
    assert payload["scanned_symbols"] == 2000
    assert payload["quote_coverage"]["verdict"] == "complete"
    assert payload["quote_coverage"]["initial_call_count"] == 20
    assert payload["quote_coverage"]["recovery_call_count"] == 3
    assert payload["quote_coverage"]["max_recovery_calls"] == 3
    assert payload["quote_coverage"]["max_concurrency"] == 1
    assert payload["quote_coverage"]["initial_batch_delay_seconds"] == 0.25
    assert payload["quote_coverage"]["recovery_delay_seconds"] == 1.0
    assert sum(attempts.values()) == 23
