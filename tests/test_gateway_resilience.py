from __future__ import annotations

import datetime as dt

import httpx
import pytest
from schwab_gateway_sdk.client import (
    GatewayAuthenticationError,
    GatewayCapacityError,
    GatewayResponseError,
)

from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import GatewayEquityDataProvider, StaleGatewayDataError

NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def settings() -> AppSettings:
    return AppSettings.model_validate(
        {"SCHWAB_GATEWAY_URL": "https://gateway.internal", "SCHWAB_GATEWAY_API_KEY": "x"}
    )


def quote(symbol: str, *, stale: bool = False) -> dict:
    return {
        "symbol": symbol, "event_timestamp": NOW, "gateway_received_at": NOW,
        "source": "test", "session": "regular", "last": 10.0, "volume": 1_000_000,
        "close": 9.0, "net_percent_change": 11.1, "stale": stale,
        "age_seconds": 999.0 if stale else 0.0, "data_quality_flags": [],
    }


async def _provider(handler, **kwargs):
    http = httpx.AsyncClient(
        base_url="https://gateway.internal", transport=httpx.MockTransport(handler)
    )
    return http, GatewayEquityDataProvider(build_gateway_client(settings(), client=http), **kwargs)


async def test_quotes_batch_at_exact_100_boundary() -> None:
    sizes = []
    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        sizes.append(len(symbols))
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )
    http, provider = await _provider(handler)
    try:
        result = await provider.get_equity_quotes([f"S{i}" for i in range(201)])
    finally:
        await http.aclose()
    assert sorted(sizes) == [1, 100, 100]
    assert len(result) == 201


@pytest.mark.parametrize(
    "status,error", [(401, GatewayAuthenticationError), (429, GatewayCapacityError)]
)
async def test_auth_and_throttling_failures(status, error) -> None:
    http, provider = await _provider(lambda request: httpx.Response(status), max_attempts=1)
    try:
        with pytest.raises(error):
            await provider.get_equity_quotes(["AAPL"])
    finally:
        await http.aclose()


async def test_transient_failure_retries_then_succeeds() -> None:
    attempts = 0
    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"schema_version": "1.0", "quotes": [quote("AAPL")]})
    http, provider = await _provider(handler, retry_backoff_seconds=0)
    try:
        assert "AAPL" in await provider.get_equity_quotes(["AAPL"])
    finally:
        await http.aclose()
    assert attempts == 3


async def test_malformed_and_partial_quote_results_are_explicit(caplog) -> None:
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"schema_version": "1.0", "quotes": [quote("AAPL")]})
        return httpx.Response(200, json={"bad": "contract"})
    http, provider = await _provider(handler)
    try:
        result = await provider.get_equity_quotes(["AAPL", "MSFT"])
        assert set(result) == {"AAPL"}
        assert "MSFT" in caplog.text
        with pytest.raises(GatewayResponseError):
            await provider.get_equity_quotes(["BROKEN"])
    finally:
        await http.aclose()


async def test_stale_quote_is_rejected_and_stale_history_fails_closed() -> None:
    def handler(request):
        if request.url.path == "/v1/quotes":
            return httpx.Response(
                200,
                json={"schema_version": "1.0", "quotes": [quote("AAPL", stale=True)]},
            )
        return httpx.Response(200, json={"schema_version": "1.0", "history": {
            "symbol": "AAPL", "frequency": "daily", "bars": [], "event_timestamp": NOW,
            "gateway_received_at": NOW, "source": "test", "stale": True,
            "age_seconds": 999.0, "data_quality_flags": []}})
    http, provider = await _provider(handler)
    try:
        assert await provider.get_equity_quotes(["AAPL"]) == {}
        with pytest.raises(StaleGatewayDataError):
            await provider.get_daily_bars("AAPL", days_back=20)
    finally:
        await http.aclose()


def test_no_direct_schwab_client_is_initialized() -> None:
    import equity_scanner.gateway as gateway
    assert not hasattr(gateway, "SchwabClientWrapper")
