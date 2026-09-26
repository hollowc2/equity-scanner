from __future__ import annotations

import datetime as dt

import httpx
import pytest
from schwab_gateway_sdk.client import GatewayUnavailableError

from equity_scanner import refresh_universes
from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import GatewayEquityDataProvider

NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def _quote(symbol: str) -> dict:
    return {
        "symbol": symbol, "event_timestamp": NOW, "gateway_received_at": NOW,
        "source": "test", "session": "regular", "last": 20.0, "volume": 1_000_000,
        "close": 19.0, "net_percent_change": 5.0, "stale": False, "age_seconds": 0.0,
        "data_quality_flags": [],
    }


async def _fetch(symbols: list[str], *, rejected: set[str], batch_size: int = 4):
    requests: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = request.url.params["symbols"].split(",")
        requests.append(batch)
        if rejected & set(batch):
            return httpx.Response(503, json={"detail": "upstream unavailable"})
        return httpx.Response(
            200, json={"schema_version": "1.0", "quotes": [_quote(s) for s in batch]}
        )

    settings = AppSettings.model_validate(
        {"SCHWAB_GATEWAY_URL": "https://gateway.internal", "SCHWAB_GATEWAY_API_KEY": "x"}
    )
    http = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    provider = GatewayEquityDataProvider(
        build_gateway_client(settings, client=http), max_attempts=1
    )
    try:
        result = await refresh_universes.fetch_seed_quotes(
            provider, symbols, batch_size=batch_size, concurrency=1
        )
    finally:
        await http.aclose()
    return result, requests


async def test_seed_quotes_isolate_a_rejected_symbol_instead_of_aborting():
    symbols = [f"S{i:02d}" for i in range(40)]

    (quotes, unquotable), requests = await _fetch(symbols, rejected={"S05"})

    assert unquotable == ["S05"]
    assert set(quotes) == set(symbols) - {"S05"}
    # 10 initial batches, then the failed batch of 4 is bisected: 2 + 2 more calls.
    assert len(requests) == 14


async def test_seed_quotes_abort_when_many_batches_fail():
    symbols = [f"S{i:02d}" for i in range(40)]

    with pytest.raises(GatewayUnavailableError):
        await _fetch(symbols, rejected={"S01", "S05"})


async def test_seed_quotes_abort_when_too_many_symbols_are_unquotable(monkeypatch):
    monkeypatch.setattr(refresh_universes, "MAX_UNQUOTABLE_SEED_SYMBOLS", 1)
    symbols = [f"S{i:02d}" for i in range(40)]

    with pytest.raises(RuntimeError, match="failed to quote"):
        await _fetch(symbols, rejected={"S04", "S05"})


async def test_liquid_refresh_keeps_existing_files_when_average_volumes_fail(
    tmp_path, monkeypatch
):
    """Before the latest daily bar is published the gateway marks history stale, every
    average-volume fetch fails, and the refresh must not write an empty universe."""
    (tmp_path / "liquid.txt").write_text("KEEP\n")
    (tmp_path / "liquid_meta.json").write_text('{"KEEP": {}}\n')
    symbols = [f"S{i:04d}" for i in range(1200)]
    monkeypatch.setattr(
        refresh_universes, "fetch_exchange_seed_map", lambda: dict.fromkeys(symbols, "NYSE")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/quotes":
            batch = request.url.params["symbols"].split(",")
            return httpx.Response(
                200, json={"schema_version": "1.0", "quotes": [_quote(s) for s in batch]}
            )
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "history": {
                    "symbol": request.url.params["symbol"], "frequency": "daily",
                    "bars": [], "event_timestamp": NOW, "gateway_received_at": NOW,
                    "source": "test", "stale": True, "age_seconds": 164_000.0,
                    "data_quality_flags": ["stale"],
                },
            },
        )

    settings = AppSettings.model_validate(
        {"SCHWAB_GATEWAY_URL": "https://gateway.internal", "SCHWAB_GATEWAY_API_KEY": "x"}
    )
    http = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    provider = GatewayEquityDataProvider(
        build_gateway_client(settings, client=http), max_attempts=1
    )
    try:
        with pytest.raises(RuntimeError, match="Refusing to replace liquid"):
            await refresh_universes.refresh_liquid_universe(
                universe_dir=tmp_path,
                provider=provider,
                min_price=10.0,
                min_avg_volume=500_000,
                batch_size=100,
                rvol_lookback_days=20,
                rvol_fetch_concurrency=8,
            )
    finally:
        await http.aclose()

    assert (tmp_path / "liquid.txt").read_text() == "KEEP\n"
    assert (tmp_path / "liquid_meta.json").read_text() == '{"KEEP": {}}\n'
