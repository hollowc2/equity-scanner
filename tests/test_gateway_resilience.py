from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest
from schwab_gateway_sdk.client import (
    GatewayAuthenticationError,
    GatewayCapacityError,
    GatewayResponseError,
    GatewayTimeoutError,
    GatewayUnavailableError,
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


async def test_class_share_symbols_are_translated_at_gateway_boundary() -> None:
    requested = []

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        requested.extend(symbols)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(symbol) for symbol in symbols]},
        )

    http, provider = await _provider(handler)
    try:
        result = await provider.get_equity_quotes(["BF.B", "BRK.B"])
    finally:
        await http.aclose()

    assert requested == ["BF/B", "BRK/B"]
    assert set(result) == {"BF.B", "BRK.B"}
    assert result["BRK.B"].symbol == "BRK.B"


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


async def test_stale_quote_is_retained_and_stale_history_fails_closed() -> None:
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
        collection = await provider.get_equity_quote_collection(["AAPL"])
        assert collection.quotes["AAPL"].stale is True
        assert collection.coverage.stale_retained_symbols == ("AAPL",)
        assert collection.coverage.stale_retained_count == 1
        with pytest.raises(StaleGatewayDataError):
            await provider.get_daily_bars("AAPL", days_back=20)
    finally:
        await http.aclose()


async def test_failed_history_task_is_evicted_before_a_later_retry() -> None:
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        stale = calls == 1
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "history": {
                    "symbol": "AAPL",
                    "frequency": "daily",
                    "bars": [],
                    "event_timestamp": NOW,
                    "gateway_received_at": NOW,
                    "source": "test",
                    "stale": stale,
                    "age_seconds": 999.0 if stale else 0.0,
                    "data_quality_flags": [],
                },
            },
        )

    http, provider = await _provider(handler)
    try:
        with pytest.raises(StaleGatewayDataError):
            await provider.get_daily_bars("AAPL")
        assert await provider.get_daily_bars("AAPL") == []
    finally:
        await http.aclose()

    assert calls == 2


async def test_all_batches_failed_is_auditable() -> None:
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(504)

    http, provider = await _provider(handler, max_attempts=1)
    try:
        with pytest.raises(GatewayTimeoutError) as caught:
            await provider.get_equity_quote_collection([f"S{i}" for i in range(250)])
    finally:
        await http.aclose()

    collection = caught.value.quote_collection
    assert calls == 3
    assert collection.quotes == {}
    assert collection.coverage.returned_count == 0
    assert collection.coverage.unavailable_count == 250
    assert collection.coverage.failed_batch_count == 3


async def test_partial_success_has_exact_coverage_accounting() -> None:
    def handler(_request):
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote("AAPL")]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            ["AAPL", "MSFT"]
        )
    finally:
        await http.aclose()

    assert collection.coverage.requested_symbols == ("AAPL", "MSFT")
    assert collection.coverage.returned_symbols == ("AAPL",)
    assert collection.coverage.unavailable_symbols == ("MSFT",)
    assert collection.coverage.failed_batches == ()
    assert collection.coverage.verdict == "incomplete"


async def test_strict_mode_preserves_exception_type_and_completed_evidence() -> None:
    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        if symbols[0] == "S0":
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        with pytest.raises(GatewayTimeoutError) as caught:
            await provider.get_equity_quotes([f"S{i}" for i in range(101)])
    finally:
        await http.aclose()

    collection = caught.value.quote_collection
    assert collection.coverage.returned_count == 1
    assert collection.coverage.unavailable_count == 100


def _rejecting(rejected: set[str], requests: list[list[str]]):
    """Gateway behavior seen live for PSKY: Schwab omits a symbol, the gateway 502s."""

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        requests.append(symbols)
        if rejected & set(symbols):
            return httpx.Response(502, json={"detail": "upstream unavailable"})
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    return handler


async def test_unquotable_symbol_is_isolated_instead_of_failing_collection() -> None:
    requests: list[list[str]] = []
    symbols = [f"S{i:03d}" for i in range(300)]
    http, provider = await _provider(_rejecting({"S150"}, requests), max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            symbols, max_unquotable_symbols=10
        )
    finally:
        await http.aclose()

    assert set(collection.quotes) == set(symbols) - {"S150"}
    coverage = collection.coverage
    assert coverage.isolated_symbols == ("S150",)
    assert coverage.unavailable_symbols == ("S150",)
    assert coverage.returned_count == 299
    assert coverage.failed_batch_count == 1
    assert coverage.verdict == "incomplete"
    # 3 initial batches, then the failed 100 is bisected (100/50/25/12/6/3): 2 calls each.
    assert len(requests) == 3 + 12


async def test_isolation_is_opt_in() -> None:
    http, provider = await _provider(_rejecting({"S5"}, []), max_attempts=1)
    try:
        with pytest.raises(GatewayUnavailableError):
            await provider.get_equity_quote_collection([f"S{i}" for i in range(10)])
    finally:
        await http.aclose()


async def test_isolation_gives_up_when_gateway_looks_unhealthy() -> None:
    # 2 of 10 batches failing exceeds the 10% bound: not a bad symbol, a bad gateway.
    http, provider = await _provider(_rejecting({"S01", "S05"}, []), max_attempts=1)
    try:
        with pytest.raises(GatewayUnavailableError):
            await provider.get_equity_quote_collection(
                [f"S{i:02d}" for i in range(40)], batch_size=4, max_unquotable_symbols=10
            )
    finally:
        await http.aclose()


async def test_isolation_gives_up_past_the_unquotable_limit() -> None:
    http, provider = await _provider(_rejecting({"S04", "S05"}, []), max_attempts=1)
    try:
        with pytest.raises(RuntimeError, match="failed to quote"):
            await provider.get_equity_quote_collection(
                [f"S{i:02d}" for i in range(40)], batch_size=4, max_unquotable_symbols=1
            )
    finally:
        await http.aclose()


async def test_cancelling_collection_cleans_up_all_concurrent_tasks() -> None:
    started = asyncio.Event()
    active = 0
    cancelled = 0

    class BlockingClient:
        async def get_quotes(self, _symbols):
            nonlocal active, cancelled
            active += 1
            if active == 3:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled += 1

    provider = GatewayEquityDataProvider(BlockingClient(), max_attempts=1)
    task = asyncio.create_task(
        provider.get_equity_quote_collection(
            [f"S{i}" for i in range(300)], concurrency=3
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == 3


async def test_twenty_batch_collection_bounds_calls_and_concurrency() -> None:
    attempts: dict[str, int] = {}
    active = 0
    peak_active = 0

    async def handler(request):
        nonlocal active, peak_active
        symbols = request.url.params["symbols"].split(",")
        batch = symbols[0]
        attempts[batch] = attempts.get(batch, 0) + 1
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(2000)],
            concurrency=4,
        )
    finally:
        await http.aclose()

    assert collection.coverage.verdict == "complete"
    assert collection.coverage.initial_call_count == 20
    assert sum(attempts.values()) == 20
    assert peak_active == 4


def test_no_direct_schwab_client_is_initialized() -> None:
    import equity_scanner.gateway as gateway
    assert not hasattr(gateway, "SchwabClientWrapper")


async def test_class_share_daily_history_is_translated_at_gateway_boundary() -> None:
    requested = []

    def handler(request):
        requested.append(request.url.params["symbol"])
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "history": {
                    "symbol": request.url.params["symbol"], "frequency": "daily",
                    "bars": [], "event_timestamp": None, "gateway_received_at": NOW,
                    "source": "test", "stale": False, "age_seconds": None,
                    "data_quality_flags": [],
                },
            },
        )

    http, provider = await _provider(handler)
    try:
        assert await provider.get_daily_bars("BRK.B") == []
    finally:
        await http.aclose()

    assert requested == ["BRK/B"]
