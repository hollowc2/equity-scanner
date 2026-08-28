"""End-to-end proof: gateway history/movers -> ported volume helpers -> ported scanner
ranking logic produces sane output. All gateway I/O is mocked at the HTTP transport
layer (httpx.MockTransport) — no live Schwab credentials, no running gateway."""

import datetime as dt

import httpx
import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import GatewayEquityDataProvider
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import build_snapshots, rank_scan_results
from equity_scanner.volume import avg_daily_volume, fetch_avg_volumes, symbols_needing_rvol_fetch

EASTERN = dt.timezone(dt.timedelta(hours=-4))  # EDT, matches America/New_York in August
PREMARKET_AT = dt.datetime(2026, 8, 19, 7, 0, tzinfo=EASTERN)  # 07:00 ET, Wednesday
MARKET_OPEN_AT = dt.datetime(2026, 8, 19, 10, 30, tzinfo=EASTERN)  # 10:30 ET, Wednesday
GATEWAY_NOW = dt.datetime.now(dt.timezone.utc).isoformat()


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


def _history_payload(symbol: str, volumes: list[int]) -> dict:
    today = dt.date.today()
    # avg_daily_volume() keys off dt.date.today() internally, so bars must be relative to
    # the real "today", oldest-first, ending yesterday (never today itself).
    bars = [
        _bar(today - dt.timedelta(days=len(volumes) - i), 100.0, volume)
        for i, volume in enumerate(volumes)
    ]
    return {
        "schema_version": "1.0",
        "history": {
            "symbol": symbol,
            "frequency": "daily",
            "bars": bars,
            "event_timestamp": None,
            "gateway_received_at": GATEWAY_NOW,
            "source": "test",
            "stale": False,
            "age_seconds": None,
            "data_quality_flags": [],
        },
    }


# avg_daily_volume() needs >= lookback//2 (10, for the default 20-day lookback) completed
# sessions or it bails out to None, so these need 10+ bars, not just a token few.
AAPL_VOLUMES = [500_000] * 10  # avg = 500_000
MSFT_VOLUMES = [1_000_000] * 10  # avg = 1_000_000

MOVERS_UP_PAYLOAD = {
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
            },
            {
                "symbol": "AMD",
                "last_price": 150.0,
                "change": 0.3,
                "change_percent": 0.2,
                "volume": 5_000_000,
            },
        ],
        "event_timestamp": None,
        "gateway_received_at": GATEWAY_NOW,
        "source": "test",
        "stale": False,
        "age_seconds": None,
        "data_quality_flags": [],
    },
}

MOVERS_DOWN_PAYLOAD = {
    "schema_version": "1.0",
    "movers": {
        "index": "NASDAQ",
        "direction": "down",
        "movers": [
            {
                "symbol": "INTC",
                "last_price": 20.0,
                "change": -1.5,
                "change_percent": -7.0,
                "volume": 15_000_000,
            },
        ],
        "event_timestamp": None,
        "gateway_received_at": GATEWAY_NOW,
        "source": "test",
        "stale": False,
        "age_seconds": None,
        "data_quality_flags": [],
    },
}


def _quote(
    *,
    session: str | None,
    close: float,
    last: float,
    net_percent_change: float,
    volume: int,
) -> QuoteV1:
    """A flat, already session-resolved gateway quote — see scanner.py's module
    docstring on why this replaced ButterflyGuy's two-payload {"quote", "extended"}
    shape. `net_percent_change` is always sourced from the regular session by the
    gateway (regardless of which session `session` reports), which is what lets
    prior_day_pct stay distinct from session_gap_pct even when `session=="extended"`."""
    return QuoteV1(
        symbol="TEST",
        gateway_received_at=dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc),
        source="test",
        session=session,
        close=close,
        last=last,
        net_percent_change=net_percent_change,
        volume=volume,
        stale=False,
    )


QUOTES = {
    # +8% premarket gap; volume alone (there's only one figure now, see scanner.py's
    # module docstring) clears the 500k min_volume filter.
    "AAPL": _quote(
        session="extended", close=100.0, last=108.0, net_percent_change=0.2, volume=600_000
    ),
    # -6% premarket gap.
    "MSFT": _quote(
        session="extended", close=200.0, last=188.0, net_percent_change=-0.5, volume=550_000
    ),
    # No extended-session data at all (session stayed "regular"): +0.2% is too small a
    # gap regardless, and no premarket volume means no rvol fetch.
    "GOOG": _quote(
        session="regular", close=150.0, last=150.3, net_percent_change=0.2, volume=520_000
    ),
    # Extended session won but with zero volume: fails min_volume (0 < 500k) and is
    # excluded from the rvol fetch (needs volume > 0), both from the same figure.
    "TSLA": _quote(
        session="extended", close=50.0, last=53.0, net_percent_change=0.2, volume=0
    ),
}

SYMBOL_MAP = {symbol: {"sp500"} for symbol in QUOTES}


@pytest.fixture
async def gateway_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/history":
            symbol = request.url.params["symbol"]
            volumes = {"AAPL": AAPL_VOLUMES, "MSFT": MSFT_VOLUMES}[symbol]
            return httpx.Response(200, json=_history_payload(symbol, volumes))
        if request.url.path == "/v1/movers":
            direction = request.url.params["direction"]
            payload = MOVERS_UP_PAYLOAD if direction == "up" else MOVERS_DOWN_PAYLOAD
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://gateway.internal", transport=transport)
    gateway = build_gateway_client(make_settings(), client=client)
    try:
        yield gateway
    finally:
        await client.aclose()


async def test_gateway_backed_scan_produces_sane_ranked_output(gateway_client) -> None:
    provider = GatewayEquityDataProvider(gateway_client)
    settings = EquityScanSettings(include_movers=True)

    rvol_symbols = symbols_needing_rvol_fetch(QUOTES, in_premarket=True)
    # GOOG/TSLA excluded: no/irrelevant premarket volume signal
    assert rvol_symbols == ["AAPL", "MSFT"]

    avg_volumes = await fetch_avg_volumes(provider, rvol_symbols)
    assert avg_volumes == {
        "AAPL": avg_daily_volume([_candle(v) for v in AAPL_VOLUMES]),
        "MSFT": avg_daily_volume([_candle(v) for v in MSFT_VOLUMES]),
    }

    rejected_symbols: dict[str, int] = {}
    snapshots = build_snapshots(
        QUOTES,
        SYMBOL_MAP,
        settings,
        avg_volumes=avg_volumes,
        in_premarket=True,
        generated_at=PREMARKET_AT,
        rejected_symbols=rejected_symbols,
    )

    by_symbol = {snap.symbol: snap for snap in snapshots}
    assert set(by_symbol) == {"AAPL", "MSFT", "GOOG"}  # TSLA filtered out
    assert rejected_symbols == {"filter_failed": 1}

    assert by_symbol["AAPL"].rvol == pytest.approx(600_000 / 500_000)
    assert by_symbol["MSFT"].rvol == pytest.approx(550_000 / 1_000_000)
    assert by_symbol["GOOG"].rvol is None  # no premarket volume, no avg_volume fetched

    movers_up = await provider.get_market_movers("NASDAQ", sort_order="PERCENT_CHANGE_UP")
    movers_down = await provider.get_market_movers("NASDAQ", sort_order="PERCENT_CHANGE_DOWN")

    premarket_results = rank_scan_results(
        snapshots,
        settings=settings,
        movers_up=movers_up,
        movers_down=movers_down,
        market_context=[],
        scanned_symbols=len(QUOTES),
        generated_at=PREMARKET_AT,
    )

    assert premarket_results.matched_symbols == 3
    assert premarket_results.show_premarket is True
    assert premarket_results.show_movers is False  # movers only show once the market is open
    assert [s.symbol for s in premarket_results.premarket_gainers] == ["AAPL"]
    assert [s.symbol for s in premarket_results.premarket_losers] == ["MSFT"]
    assert premarket_results.opening_focus, "expected at least one opening-focus candidate"
    assert premarket_results.opening_focus[0].snapshot.symbol in {"AAPL", "MSFT"}

    open_results = rank_scan_results(
        snapshots,
        settings=settings,
        movers_up=movers_up,
        movers_down=movers_down,
        market_context=[],
        scanned_symbols=len(QUOTES),
        generated_at=MARKET_OPEN_AT,
    )

    assert open_results.show_premarket is False
    assert open_results.show_movers is True
    # AMD's 0.2% move is below movers_min_abs_pct (1.0) and gets dropped
    assert [m["symbol"] for m in open_results.movers_up] == ["NVDA"]
    assert [m["symbol"] for m in open_results.movers_down] == ["INTC"]


def test_stale_quote_is_visible_as_data_quality_flag() -> None:
    settings = EquityScanSettings()
    stale_quote = QUOTES["AAPL"].model_copy(
        update={"stale": True, "age_seconds": 900.0}
    )

    snapshots = build_snapshots(
        {"AAPL": stale_quote},
        {"AAPL": {"sp500"}},
        settings,
        in_premarket=True,
        generated_at=PREMARKET_AT,
    )

    assert len(snapshots) == 1
    assert snapshots[0].data_quality_flags == ("gateway_stale",)


def _candle(volume: int) -> dict:
    return {"datetime": 0, "close": 100.0, "volume": volume}
