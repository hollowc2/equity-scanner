"""Gateway-backed implementation of ButterflyGuy's PriceHistoryProvider/
MarketMoversProvider Protocols (see Butterflyguy/src/butterfly_guy/data/providers.py).

Only the two surfaces equity_scan actually needs: daily bars (for rvol/prior-day-change)
and market movers. Not a general-purpose gateway client wrapper.
"""

from __future__ import annotations

import asyncio
from typing import Any

from schwab_gateway_sdk import GatewayMarketDataClient, QuoteV1


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

    def __init__(self, client: GatewayMarketDataClient) -> None:
        self._client = client

    async def get_daily_bars(self, symbol: str, days_back: int | None = None) -> list[dict]:
        """Matches PriceHistoryProvider.get_daily_bars's shape. Leaves days_back unset by
        default so the gateway's own default (20 daily bars) applies — that default is
        deliberately sized for the 20-day rvol lookback these callers use."""
        response = await self._client.get_history(symbol, frequency="daily", days_back=days_back)
        return [_bar_to_candle(bar) for bar in response.history.bars]

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
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return {}
        batch_size = min(batch_size, 100)
        chunks = [
            unique_symbols[i : i + batch_size] for i in range(0, len(unique_symbols), batch_size)
        ]
        sem = asyncio.Semaphore(concurrency)
        quotes: dict[str, QuoteV1] = {}

        async def _fetch(chunk: list[str]) -> None:
            async with sem:
                response = await self._client.get_quotes(chunk)
                for quote in response.quotes:
                    quotes[quote.symbol] = quote

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
        response = await self._client.get_movers(index, direction=direction)
        return [_mover_to_dict(mover) for mover in response.movers.movers]
