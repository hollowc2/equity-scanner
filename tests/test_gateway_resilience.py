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


async def test_parity_collection_recovers_one_timed_out_batch_without_losing_others() -> None:
    attempts: dict[str, int] = {}

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        batch = symbols[0]
        attempts[batch] = attempts.get(batch, 0) + 1
        if batch == "S100" and attempts[batch] == 1:
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(201)],
            mode="parity-bounded-recovery",
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert len(collection.quotes) == 201
    assert collection.coverage.complete is True
    assert collection.coverage.initial_call_count == 3
    assert collection.coverage.recovery_call_count == 1
    assert collection.coverage.failed_batch_count == 0
    assert sum(attempts.values()) == 4


async def test_parity_collection_multiple_failed_batches_are_not_retried() -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        symbols = request.url.params["symbols"].split(",")
        if symbols[0] in {"S0", "S100"}:
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(201)],
            mode="parity-bounded-recovery",
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert calls == 3
    assert set(collection.quotes) == {"S200"}
    assert collection.coverage.failed_batch_count == 2
    assert collection.coverage.unavailable_count == 200
    assert collection.coverage.recovery_call_count == 0
    assert [failure.error_type for failure in collection.coverage.failed_batches] == [
        "GatewayTimeoutError",
        "GatewayTimeoutError",
    ]


async def test_paced_parity_recovers_three_failed_batches_sequentially() -> None:
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
        if batch in {"S0", "S100", "S200"} and attempts[batch] == 1:
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(301)],
            concurrency=4,
            mode="parity-paced-recovery",
            initial_batch_delay_seconds=0,
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert collection.coverage.verdict == "complete"
    assert collection.coverage.initial_call_count == 4
    assert collection.coverage.recovery_call_count == 3
    assert collection.coverage.max_recovery_calls == 3
    assert collection.coverage.max_concurrency == 1
    assert sum(attempts.values()) == 7
    assert peak_active == 1


async def test_paced_parity_does_not_recover_more_than_three_failed_batches() -> None:
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(504)

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(400)],
            mode="parity-paced-recovery",
            initial_batch_delay_seconds=0,
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert calls == 4
    assert collection.coverage.failed_batch_count == 4
    assert collection.coverage.recovery_call_count == 0
    assert collection.coverage.unavailable_count == 400


async def test_paced_parity_records_a_failed_recovery_and_continues() -> None:
    attempts: dict[str, int] = {}

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        batch = symbols[0]
        attempts[batch] = attempts.get(batch, 0) + 1
        if batch == "S0" or (batch == "S100" and attempts[batch] == 1):
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(201)],
            mode="parity-paced-recovery",
            initial_batch_delay_seconds=0,
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert sum(attempts.values()) == 5
    assert collection.coverage.recovery_call_count == 2
    assert collection.coverage.failed_batch_count == 1
    assert collection.coverage.unavailable_count == 100
    assert collection.coverage.failed_batches[0].recovery_attempted is True


async def test_paced_parity_keeps_retrying_a_batch_within_recovery_attempt_budget() -> None:
    """An outage that outlasts a single recovery call should not sink the whole batch
    when the caller can afford to wait: recovery keeps retrying the same batch, with
    backoff, until it succeeds or the attempt budget is exhausted."""
    attempts: dict[str, int] = {}

    def handler(request):
        symbols = request.url.params["symbols"].split(",")
        batch = symbols[0]
        attempts[batch] = attempts.get(batch, 0) + 1
        if batch == "S0" and attempts[batch] < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(100)],
            mode="parity-paced-recovery",
            initial_batch_delay_seconds=0,
            recovery_delay_seconds=0,
            max_recovery_attempts=4,
        )
    finally:
        await http.aclose()

    assert attempts["S0"] == 3
    assert collection.coverage.verdict == "complete"
    assert collection.coverage.recovery_call_count == 2
    assert collection.coverage.max_recovery_attempts == 4
    assert collection.coverage.failed_batch_count == 0
    assert collection.coverage.unavailable_count == 0


async def test_paced_parity_exhausts_recovery_attempt_budget_and_stays_unavailable() -> None:
    def handler(_request):
        return httpx.Response(503)

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(100)],
            mode="parity-paced-recovery",
            initial_batch_delay_seconds=0,
            recovery_delay_seconds=0,
            max_recovery_attempts=3,
        )
    finally:
        await http.aclose()

    assert collection.coverage.recovery_call_count == 3
    assert collection.coverage.failed_batch_count == 1
    assert collection.coverage.unavailable_count == 100
    assert collection.coverage.failed_batches[0].recovery_attempted is True


async def test_paced_parity_cancellation_stops_during_recovery_delay() -> None:
    calls = 0
    initial_finished = asyncio.Event()

    class TimedOutClient:
        async def get_quotes(self, _symbols):
            nonlocal calls
            calls += 1
            initial_finished.set()
            raise GatewayTimeoutError("timed out")

    provider = GatewayEquityDataProvider(TimedOutClient(), max_attempts=1)
    task = asyncio.create_task(
        provider.get_equity_quote_collection(
            ["AAPL"],
            mode="parity-paced-recovery",
            recovery_delay_seconds=60,
        )
    )
    await initial_finished.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1


async def test_parity_collection_all_batches_failed_is_auditable() -> None:
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(504)

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(250)],
            mode="parity-bounded-recovery",
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert calls == 3
    assert collection.quotes == {}
    assert collection.coverage.returned_count == 0
    assert collection.coverage.unavailable_count == 250
    assert collection.coverage.failed_batch_count == 3
    assert collection.coverage.recovery_call_count == 0


async def test_partial_success_has_exact_coverage_accounting() -> None:
    def handler(_request):
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote("AAPL")]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            ["AAPL", "MSFT"], mode="parity-bounded-recovery"
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


async def test_twenty_batch_mock_proof_bounds_calls_retries_and_concurrency() -> None:
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
        if batch == "S900" and attempts[batch] == 1:
            return httpx.Response(504)
        return httpx.Response(
            200,
            json={"schema_version": "1.0", "quotes": [quote(s) for s in symbols]},
        )

    http, provider = await _provider(handler, max_attempts=1)
    try:
        collection = await provider.get_equity_quote_collection(
            [f"S{i}" for i in range(2000)],
            concurrency=4,
            mode="parity-bounded-recovery",
            recovery_delay_seconds=0,
        )
    finally:
        await http.aclose()

    assert collection.coverage.verdict == "complete"
    assert collection.coverage.initial_call_count == 20
    assert collection.coverage.recovery_call_count == 1
    assert sum(attempts.values()) == 21
    assert peak_active == 4


def test_no_direct_schwab_client_is_initialized() -> None:
    import equity_scanner.gateway as gateway
    assert not hasattr(gateway, "SchwabClientWrapper")
