"""Gateway-backed implementation of ButterflyGuy's PriceHistoryProvider/
MarketMoversProvider Protocols (see Butterflyguy/src/butterfly_guy/data/providers.py).

Only the two surfaces equity_scan actually needs: daily bars (for rvol/prior-day-change)
and market movers. Not a general-purpose gateway client wrapper.
"""

from __future__ import annotations

from typing import Any

from schwab_gateway_sdk import GatewayMarketDataClient


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
