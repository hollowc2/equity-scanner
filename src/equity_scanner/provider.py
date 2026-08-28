"""Gateway-backed implementation of ButterflyGuy's PriceHistoryProvider/
MarketMoversProvider Protocols (see Butterflyguy/src/butterfly_guy/data/providers.py).

Only the two surfaces equity_scan actually needs: daily bars (for rvol/prior-day-change)
and market movers. Not a general-purpose gateway client wrapper.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

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
                        symbol,
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
        requested_symbols = list(dict.fromkeys(symbols))
        if not requested_symbols:
            return {}
        gateway_to_requested: dict[str, str] = {}
        for symbol in requested_symbols:
            gateway_to_requested.setdefault(_gateway_symbol(symbol), symbol)
        gateway_symbols = list(gateway_to_requested)
        batch_size = min(batch_size, 100)
        chunks = [
            gateway_symbols[i : i + batch_size]
            for i in range(0, len(gateway_symbols), batch_size)
        ]
        sem = asyncio.Semaphore(concurrency)
        quotes: dict[str, QuoteV1] = {}

        async def _fetch(chunk: list[str]) -> None:
            async with sem:
                response = await self._retry(lambda: self._client.get_quotes(chunk))
                stale_symbols: list[str] = []
                for quote in response.quotes:
                    requested_symbol = gateway_to_requested.get(quote.symbol, quote.symbol)
                    if quote.stale:
                        # Quote freshness is based on the selected trade event. In
                        # premarket, an otherwise usable quote can therefore be
                        # marked stale simply because the symbol has not traded
                        # recently. Preserve it for the scanner's normal field and
                        # liquidity validation, while retaining the SDK's stale bit
                        # and exposing it as a data-quality flag downstream.
                        stale_symbols.append(requested_symbol)
                    if requested_symbol != quote.symbol:
                        quote = quote.model_copy(update={"symbol": requested_symbol})
                    quotes[requested_symbol] = quote
                if stale_symbols:
                    log.info(
                        "gateway_quote_batch_stale_retained count=%d",
                        len(stale_symbols),
                    )
                returned = {quote.symbol for quote in response.quotes}
                missing = sorted(
                    gateway_to_requested[symbol] for symbol in set(chunk) - returned
                )
                if missing:
                    log.warning("gateway_quote_batch_partial missing_symbols=%s", ",".join(missing))

        await asyncio.gather(*(_fetch(chunk) for chunk in chunks))
        return quotes

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
