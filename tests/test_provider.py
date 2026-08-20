import datetime as dt

import httpx
import pytest

from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import GatewayEquityDataProvider

NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def make_settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "SCHWAB_GATEWAY_URL": "https://gateway.internal",
            "SCHWAB_GATEWAY_API_KEY": "test-key",
        }
    )


def _bar(day: dt.date, close: float, volume: int) -> dict:
    return {
        "timestamp": dt.datetime.combine(day, dt.time(16, 0), tzinfo=dt.timezone.utc).isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


HISTORY_PAYLOAD = {
    "schema_version": "1.0",
    "history": {
        "symbol": "AAPL",
        "frequency": "daily",
        "bars": [
            _bar(dt.date(2026, 8, 10), 100.0, 1_000_000),
            _bar(dt.date(2026, 8, 11), 101.0, 1_100_000),
            _bar(dt.date(2026, 8, 12), 99.0, 900_000),
        ],
        "event_timestamp": None,
        "gateway_received_at": NOW,
        "source": "test",
        "stale": False,
        "age_seconds": None,
        "data_quality_flags": [],
    },
}

MOVERS_PAYLOAD = {
    "schema_version": "1.0",
    "movers": {
        "index": "NASDAQ",
        "direction": "up",
        "movers": [
            {
                "symbol": "NVDA",
                "last_price": 900.0,
                "change": 45.0,
                "change_percent": 5.3,
                "volume": 20_000_000,
            }
        ],
        "event_timestamp": None,
        "gateway_received_at": NOW,
        "source": "test",
        "stale": False,
        "age_seconds": None,
        "data_quality_flags": [],
    },
}


@pytest.fixture
async def gateway_client():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["last"] = request
        if request.url.path == "/v1/history":
            return httpx.Response(200, json=HISTORY_PAYLOAD)
        if request.url.path == "/v1/movers":
            return httpx.Response(200, json=MOVERS_PAYLOAD)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://gateway.internal", transport=transport)
    gateway = build_gateway_client(make_settings(), client=client)
    try:
        yield gateway, captured
    finally:
        await client.aclose()


async def test_get_daily_bars_converts_gateway_bars_to_candle_dicts(gateway_client) -> None:
    gateway, captured = gateway_client
    provider = GatewayEquityDataProvider(gateway)

    candles = await provider.get_daily_bars("AAPL")

    assert len(candles) == 3
    assert candles[0]["close"] == 100.0
    assert candles[0]["volume"] == 1_000_000
    assert isinstance(candles[0]["datetime"], int)
    # days_back left unset so the gateway's own default (20, matching the rvol lookback) applies
    assert "days_back" not in captured["last"].url.params


async def test_get_market_movers_converts_direction_and_shape(gateway_client) -> None:
    gateway, captured = gateway_client
    provider = GatewayEquityDataProvider(gateway)

    movers = await provider.get_market_movers("NASDAQ", sort_order="PERCENT_CHANGE_UP")

    assert captured["last"].url.params["direction"] == "up"
    assert movers == [
        {
            "symbol": "NVDA",
            "lastPrice": 900.0,
            "change": 45.0,
            "changePercent": 5.3,
            "volume": 20_000_000,
        }
    ]


async def test_get_market_movers_maps_down_sort_order(gateway_client) -> None:
    gateway, captured = gateway_client
    provider = GatewayEquityDataProvider(gateway)

    await provider.get_market_movers("NASDAQ", sort_order="PERCENT_CHANGE_DOWN")

    assert captured["last"].url.params["direction"] == "down"
