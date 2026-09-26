"""Gateway-backed implementation of ButterflyGuy's PriceHistoryProvider/
MarketMoversProvider Protocols (see Butterflyguy/src/butterfly_guy/data/providers.py).

Only the two surfaces equity_scan actually needs: daily bars (for rvol/prior-day-change)
and market movers. Not a general-purpose gateway client wrapper.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from schwab_gateway_sdk import GatewayMarketDataClient, QuoteV1
from schwab_gateway_sdk.client import (
    GatewayCapacityError,
    GatewayTimeoutError,
    GatewayUnavailableError,
)

log = logging.getLogger(__name__)


def _gateway_symbol(symbol: str) -> str:
    """Translate class-share notation to the form accepted by Schwab/Gateway."""
    return symbol.replace(".", "/")


class StaleGatewayDataError(RuntimeError):
    """Raised when the gateway explicitly marks a response stale."""


QuoteCoverageMode = Literal[
    "strict",
    "parity-bounded-recovery",
    "parity-paced-recovery",
]


@dataclass(frozen=True)
class QuoteBatchFailure:
    batch_index: int
    symbols: tuple[str, ...]
    error_type: str
    error_message: str
    recovery_attempted: bool = False


@dataclass(frozen=True)
class QuoteCoverage:
    mode: QuoteCoverageMode
    requested_count: int
    returned_count: int
    stale_retained_count: int
    unavailable_count: int
    failed_batch_count: int
    requested_symbols: tuple[str, ...]
    returned_symbols: tuple[str, ...]
    stale_retained_symbols: tuple[str, ...]
    unavailable_symbols: tuple[str, ...]
    failed_batches: tuple[QuoteBatchFailure, ...]
    initial_call_count: int
    recovery_call_count: int
    max_concurrency: int
    complete: bool
    verdict: Literal["complete", "incomplete"]
    max_recovery_calls: int = 0
    initial_batch_delay_seconds: float = 0.0
    recovery_delay_seconds: float = 0.0
    max_recovery_attempts: int = 1


@dataclass(frozen=True)
class QuoteCollection:
    quotes: dict[str, QuoteV1]
    coverage: QuoteCoverage


def _bar_to_candle(bar: Any) -> dict[str, Any]:
    """Gateway PriceBarV1 -> the {"datetime": epoch_ms, "close", "volume", ...} shape
    volume.py's avg_daily_volume/prior_session_pct_change expect (ButterflyGuy's Schwab
    client returns candles in this shape)."""
    return {
        "datetime": int(bar.timestamp.timestamp() * 1000),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _mover_to_dict(mover: Any) -> dict[str, Any]:
    """Gateway MoverV1 -> the dict shape scanner.py's filter_movers/_mover_change_pct
    expect (item["symbol"], item["changePercent"], item["change"])."""
    return {
        "symbol": mover.symbol,
        "lastPrice": mover.last_price,
        "change": mover.change,
        "changePercent": mover.change_percent,
        "volume": mover.volume,
    }


class GatewayEquityDataProvider:
    """Delegates to GatewayMarketDataClient without owning its lifecycle."""

    def __init__(
        self,
        client: GatewayMarketDataClient,
        *,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        self._client = client
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._daily_bars_tasks: dict[
            tuple[str, int | None], asyncio.Task[tuple[dict, ...]]
        ] = {}

    async def _retry(self, operation):
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await operation()
            except (GatewayCapacityError, GatewayTimeoutError, GatewayUnavailableError):
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(self._retry_backoff_seconds * (2 ** (attempt - 1)))
        raise AssertionError("retry loop exhausted")

    @staticmethod
    def _require_fresh(payload: Any, surface: str) -> None:
        if payload.stale:
            raise StaleGatewayDataError(
                f"gateway returned stale {surface} data (age_seconds={payload.age_seconds})"
            )

    async def get_daily_bars(self, symbol: str, days_back: int | None = 20) -> list[dict]:
        """Fetch and cache one daily-history window for this provider's lifetime.

        A provider is created per scanner run, so this coalesces overlapping RVOL and
        prior-day requests without carrying market data across runs. Failed tasks are
        evicted so a later phase can retry normally.
        """
        key = (symbol, days_back)
        task = self._daily_bars_tasks.get(key)
        if task is None:

            async def _load() -> tuple[dict, ...]:
                response = await self._retry(
                    lambda: self._client.get_history(
                        _gateway_symbol(symbol),
                        frequency="daily",
                        days_back=days_back,
                    )
                )
                self._require_fresh(response.history, f"history for {symbol}")
                return tuple(_bar_to_candle(bar) for bar in response.history.bars)

            task = asyncio.create_task(_load())
            self._daily_bars_tasks[key] = task

        try:
            bars = await task
        except BaseException:
            if self._daily_bars_tasks.get(key) is task:
                self._daily_bars_tasks.pop(key, None)
            raise
        return [dict(bar) for bar in bars]

    async def get_equity_quotes(
        self,
        symbols: list[str],
        *,
        batch_size: int = 100,
        concurrency: int = 4,
    ) -> dict[str, QuoteV1]:
        """symbol -> flat gateway quote, for universes.py's liquidity filter and
        scanner.py's parse_equity_quote. Batches into chunks of at most 100 (the
        gateway's /v1/quotes per-request cap, MAX_SYMBOLS in SchwabGateway's api.py)
        and dedupes, since ButterflyGuy's raw Schwab client allowed larger/duplicate
        batches that the gateway's contract does not accept."""
        collection = await self.get_equity_quote_collection(
            symbols,
            batch_size=batch_size,
            concurrency=concurrency,
            mode="strict",
        )
        return collection.quotes

    async def get_equity_quote_collection(
        self,
        symbols: list[str],
        *,
        batch_size: int = 100,
        concurrency: int = 4,
        mode: QuoteCoverageMode = "strict",
        recovery_delay_seconds: float = 1.0,
        initial_batch_delay_seconds: float = 0.25,
        max_recovery_batches: int = 3,
        max_recovery_attempts: int = 1,
    ) -> QuoteCollection:
        """Collect bounded quote batches and retain auditable coverage evidence.

        ``strict`` preserves the normal fail-closed behavior, but only after every
        scheduled batch has settled and its evidence has been retained.
        ``parity-bounded-recovery`` makes at most one extra, sequential gateway call,
        and only when exactly one initial batch has a transient gateway failure.
        ``parity-paced-recovery`` serializes and spaces the initial calls, then makes
        up to ``max_recovery_attempts`` sequential recovery calls (with exponential
        backoff off ``recovery_delay_seconds``) per transient failed batch, stopping
        early on success, when no more than ``max_recovery_batches`` failed initially.
        Incomplete parity collections are returned so the report and comparator can
        fail with coverage evidence instead of losing all completed work.
        """
        if mode not in {
            "strict",
            "parity-bounded-recovery",
            "parity-paced-recovery",
        }:
            raise ValueError(f"unsupported quote coverage mode: {mode}")
        if concurrency < 1:
            raise ValueError("quote concurrency must be at least 1")
        if recovery_delay_seconds < 0:
            raise ValueError("quote recovery delay must not be negative")
        if initial_batch_delay_seconds < 0:
            raise ValueError("initial quote batch delay must not be negative")
        if max_recovery_batches < 1:
            raise ValueError("maximum recovery batches must be at least 1")
        if max_recovery_attempts < 1:
            raise ValueError("maximum recovery attempts must be at least 1")

        paced = mode == "parity-paced-recovery"
        effective_concurrency = 1 if paced else concurrency
        effective_initial_delay = initial_batch_delay_seconds if paced else 0.0
        if paced:
            max_recovery_calls = max_recovery_batches
        elif mode == "parity-bounded-recovery":
            max_recovery_calls = 1
        else:
            max_recovery_calls = 0

        requested_symbols = list(dict.fromkeys(symbols))
        if not requested_symbols:
            coverage = QuoteCoverage(
                mode=mode,
                requested_count=0,
                returned_count=0,
                stale_retained_count=0,
                unavailable_count=0,
                failed_batch_count=0,
                requested_symbols=(),
                returned_symbols=(),
                stale_retained_symbols=(),
                unavailable_symbols=(),
                failed_batches=(),
                initial_call_count=0,
                recovery_call_count=0,
                max_concurrency=effective_concurrency,
                complete=True,
                verdict="complete",
                max_recovery_calls=max_recovery_calls,
                initial_batch_delay_seconds=effective_initial_delay,
                recovery_delay_seconds=recovery_delay_seconds,
                max_recovery_attempts=max_recovery_attempts,
            )
            return QuoteCollection(quotes={}, coverage=coverage)
        gateway_to_requested: dict[str, str] = {}
        for symbol in requested_symbols:
            gateway_to_requested.setdefault(_gateway_symbol(symbol), symbol)
        gateway_symbols = list(gateway_to_requested)
        if batch_size < 1:
            raise ValueError("quote batch size must be at least 1")
        batch_size = min(batch_size, 100)
        chunks = [
            gateway_symbols[i : i + batch_size]
            for i in range(0, len(gateway_symbols), batch_size)
        ]
        sem = asyncio.Semaphore(effective_concurrency)
        transient_errors = (GatewayCapacityError, GatewayTimeoutError, GatewayUnavailableError)

        @dataclass(frozen=True)
        class _BatchResult:
            batch_index: int
            chunk: tuple[str, ...]
            quotes: tuple[QuoteV1, ...]

        async def _fetch(batch_index: int, chunk: list[str]) -> _BatchResult:
            async with sem:
                response = await self._retry(lambda: self._client.get_quotes(chunk))
                return _BatchResult(batch_index, tuple(chunk), tuple(response.quotes))

        if paced:
            settled: list[_BatchResult | BaseException] = []
            for batch_index, chunk in enumerate(chunks):
                if batch_index and effective_initial_delay:
                    await asyncio.sleep(effective_initial_delay)
                try:
                    settled.append(await _fetch(batch_index, chunk))
                except Exception as exc:
                    settled.append(exc)
        else:
            tasks = [
                asyncio.create_task(_fetch(batch_index, chunk))
                for batch_index, chunk in enumerate(chunks)
            ]
            try:
                settled = await asyncio.gather(*tasks, return_exceptions=True)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        batch_results: dict[int, _BatchResult] = {}
        initial_failures: dict[int, BaseException] = {}
        for batch_index, result in enumerate(settled):
            if isinstance(result, BaseException):
                initial_failures[batch_index] = result
            else:
                batch_results[batch_index] = result

        recovery_call_count = 0
        recovery_attempted: set[int] = set()
        transient_failures = [
            batch_index
            for batch_index, error in initial_failures.items()
            if isinstance(error, transient_errors)
        ]
        recovery_batches: list[int] = []
        if (
            mode == "parity-bounded-recovery"
            and len(initial_failures) == 1
            and len(transient_failures) == 1
        ):
            recovery_batches = transient_failures
        elif (
            paced
            and 0 < len(initial_failures) <= max_recovery_batches
            and len(transient_failures) == len(initial_failures)
        ):
            recovery_batches = sorted(transient_failures)

        for batch_index in recovery_batches:
            recovery_attempted.add(batch_index)
            for attempt in range(1, max_recovery_attempts + 1):
                recovery_call_count += 1
                delay = recovery_delay_seconds * (2 ** (attempt - 1))
                log.warning(
                    "gateway_quote_batch_recovery_scheduled batch_id=%d symbol_count=%d "
                    "delay_seconds=%.3f recovery_call=%d max_recovery_calls=%d attempt=%d "
                    "max_recovery_attempts=%d",
                    batch_index,
                    len(chunks[batch_index]),
                    delay,
                    recovery_call_count,
                    max_recovery_calls,
                    attempt,
                    max_recovery_attempts,
                )
                if delay:
                    await asyncio.sleep(delay)
                try:
                    # Deliberately bypass provider retries. The named policy owns this
                    # explicit, globally bounded recovery call budget.
                    response = await self._client.get_quotes(chunks[batch_index])
                    batch_results[batch_index] = _BatchResult(
                        batch_index,
                        tuple(chunks[batch_index]),
                        tuple(response.quotes),
                    )
                    initial_failures.pop(batch_index)
                    log.info(
                        "gateway_quote_batch_recovery_succeeded batch_id=%d symbol_count=%d "
                        "attempt=%d",
                        batch_index,
                        len(chunks[batch_index]),
                        attempt,
                    )
                    break
                except Exception as exc:
                    initial_failures[batch_index] = exc
                    log.warning(
                        "gateway_quote_batch_recovery_failed batch_id=%d symbol_count=%d "
                        "error_type=%s error=%s attempt=%d max_recovery_attempts=%d",
                        batch_index,
                        len(chunks[batch_index]),
                        type(exc).__name__,
                        exc,
                        attempt,
                        max_recovery_attempts,
                    )

        quotes: dict[str, QuoteV1] = {}
        stale_symbols: set[str] = set()
        for batch_index in sorted(batch_results):
            result = batch_results[batch_index]
            returned_gateway_symbols: set[str] = set()
            batch_stale_symbols: list[str] = []
            for quote in result.quotes:
                if quote.symbol not in result.chunk:
                    log.warning(
                        "gateway_quote_batch_unexpected_symbol batch_id=%d symbol=%s",
                        batch_index,
                        quote.symbol,
                    )
                    continue
                returned_gateway_symbols.add(quote.symbol)
                requested_symbol = gateway_to_requested.get(quote.symbol, quote.symbol)
                if quote.stale:
                    # Premarket quotes may be usable but stale merely because the
                    # selected trade event is old. Preserve and label them.
                    stale_symbols.add(requested_symbol)
                    batch_stale_symbols.append(requested_symbol)
                if requested_symbol != quote.symbol:
                    quote = quote.model_copy(update={"symbol": requested_symbol})
                quotes[requested_symbol] = quote
            if batch_stale_symbols:
                log.info(
                    "gateway_quote_batch_stale_retained batch_id=%d count=%d",
                    batch_index,
                    len(batch_stale_symbols),
                )
            missing = sorted(
                gateway_to_requested[symbol]
                for symbol in set(result.chunk) - returned_gateway_symbols
            )
            if missing:
                log.warning(
                    "gateway_quote_batch_partial batch_id=%d missing_symbols=%s",
                    batch_index,
                    ",".join(missing),
                )

        failures = tuple(
            QuoteBatchFailure(
                batch_index=batch_index,
                symbols=tuple(gateway_to_requested[symbol] for symbol in chunks[batch_index]),
                error_type=type(error).__name__,
                error_message=str(error),
                recovery_attempted=batch_index in recovery_attempted,
            )
            for batch_index, error in sorted(initial_failures.items())
        )
        for failure in failures:
            log.warning(
                "gateway_quote_batch_failed batch_id=%d symbol_count=%d symbols=%s "
                "error_type=%s error=%s recovery_attempted=%s",
                failure.batch_index,
                len(failure.symbols),
                ",".join(failure.symbols),
                failure.error_type,
                failure.error_message,
                failure.recovery_attempted,
            )

        returned_symbols = tuple(symbol for symbol in requested_symbols if symbol in quotes)
        unavailable_symbols = tuple(symbol for symbol in requested_symbols if symbol not in quotes)
        complete = not unavailable_symbols and not failures
        coverage = QuoteCoverage(
            mode=mode,
            requested_count=len(requested_symbols),
            returned_count=len(returned_symbols),
            stale_retained_count=len(stale_symbols),
            unavailable_count=len(unavailable_symbols),
            failed_batch_count=len(failures),
            requested_symbols=tuple(requested_symbols),
            returned_symbols=returned_symbols,
            stale_retained_symbols=tuple(
                symbol for symbol in requested_symbols if symbol in stale_symbols
            ),
            unavailable_symbols=unavailable_symbols,
            failed_batches=failures,
            initial_call_count=len(chunks),
            recovery_call_count=recovery_call_count,
            max_concurrency=min(effective_concurrency, len(chunks)),
            complete=complete,
            verdict="complete" if complete else "incomplete",
            max_recovery_calls=max_recovery_calls,
            initial_batch_delay_seconds=effective_initial_delay,
            recovery_delay_seconds=recovery_delay_seconds,
            max_recovery_attempts=max_recovery_attempts,
        )
        collection = QuoteCollection(quotes=quotes, coverage=coverage)
        log.info(
            "gateway_quote_coverage mode=%s verdict=%s requested=%d returned=%d "
            "stale_retained=%d unavailable=%d failed_batches=%d initial_calls=%d "
            "recovery_calls=%d max_recovery_calls=%d max_concurrency=%d "
            "initial_batch_delay_seconds=%.3f recovery_delay_seconds=%.3f "
            "max_recovery_attempts=%d",
            mode,
            coverage.verdict,
            coverage.requested_count,
            coverage.returned_count,
            coverage.stale_retained_count,
            coverage.unavailable_count,
            coverage.failed_batch_count,
            coverage.initial_call_count,
            coverage.recovery_call_count,
            coverage.max_recovery_calls,
            coverage.max_concurrency,
            coverage.initial_batch_delay_seconds,
            coverage.recovery_delay_seconds,
            coverage.max_recovery_attempts,
        )

        if mode == "strict" and failures:
            first_error = initial_failures[failures[0].batch_index]
            # Preserve the SDK exception type while making completed evidence
            # available to strict-mode callers that choose to inspect it.
            first_error.quote_collection = collection  # type: ignore[attr-defined]
            raise first_error
        return collection

    async def get_market_movers(
        self,
        index: str,
        *,
        sort_order: str = "PERCENT_CHANGE_UP",
        frequency: int | None = None,
    ) -> list[dict[str, Any]]:
        """Matches MarketMoversProvider.get_market_movers's shape. `frequency` has no
        gateway equivalent and is accepted only for Protocol/call-site compatibility."""
        direction = "down" if "DOWN" in sort_order.upper() else "up"
        response = await self._retry(lambda: self._client.get_movers(index, direction=direction))
        self._require_fresh(response.movers, f"movers for {index}/{direction}")
        return [_mover_to_dict(mover) for mover in response.movers.movers]
