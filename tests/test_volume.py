import datetime as dt
from zoneinfo import ZoneInfo

from schwab_gateway_sdk import QuoteV1

from equity_scanner.volume import (
    avg_daily_volume,
    compute_rvol,
    fetch_avg_volumes,
    fetch_prior_day_changes,
    prior_session_pct_change,
    symbols_needing_rvol_fetch,
)

TODAY = dt.date.today()


def _ms(days_ago: int) -> int:
    day = TODAY - dt.timedelta(days=days_ago)
    return int(dt.datetime.combine(day, dt.time(16, 0)).timestamp() * 1000)


class FakeProvider:
    def __init__(self, candles_by_symbol: dict[str, list[dict]]) -> None:
        self._candles_by_symbol = candles_by_symbol
        self.calls: list[tuple[str, int | None]] = []

    async def get_daily_bars(self, symbol: str, days_back: int | None = None) -> list[dict]:
        self.calls.append((symbol, days_back))
        return self._candles_by_symbol.get(symbol, [])


def test_avg_daily_volume_excludes_today_and_zero_volume_bars() -> None:
    candles = [
        {"datetime": _ms(3), "close": 100.0, "volume": 1_000_000},
        {"datetime": _ms(2), "close": 101.0, "volume": 0},
        {"datetime": _ms(1), "close": 99.0, "volume": 900_000},
        {"datetime": _ms(0), "close": 105.0, "volume": 5_000_000},  # today, excluded
    ]

    avg = avg_daily_volume(candles, lookback=2)

    assert avg == (1_000_000 + 900_000) / 2


def test_prior_session_pct_change_uses_last_two_completed_sessions() -> None:
    candles = [
        {"datetime": _ms(3), "close": 100.0, "volume": 1_000_000},
        {"datetime": _ms(2), "close": 110.0, "volume": 1_000_000},
        {"datetime": _ms(0), "close": 999.0, "volume": 1_000_000},  # today, excluded
    ]

    pct = prior_session_pct_change(candles)

    assert pct == 10.0


def test_compute_rvol() -> None:
    assert compute_rvol(500_000, 1_000_000) == 0.5
    assert compute_rvol(0, 1_000_000) is None
    assert compute_rvol(500_000, None) is None


EASTERN = ZoneInfo("America/New_York")
PREMARKET_AT = dt.datetime(2026, 9, 28, 9, 0, tzinfo=EASTERN)


def _quote(
    *, session: str | None, volume: int | None, event_timestamp: dt.datetime | None
) -> QuoteV1:
    return QuoteV1(
        symbol="TEST",
        event_timestamp=event_timestamp,
        gateway_received_at=dt.datetime.now(dt.timezone.utc),
        source="test",
        session=session,
        volume=volume,
        stale=False,
    )


def test_symbols_needing_rvol_fetch_uses_todays_trading_not_session_label() -> None:
    today = dt.datetime(2026, 9, 28, 8, 45, tzinfo=EASTERN)
    quotes = {
        # The production shape before the open: "regular" label, premarket volume.
        "AAPL": _quote(session="regular", volume=10_000, event_timestamp=today),
        "MSFT": _quote(session="extended", volume=0, event_timestamp=today),
        # Last activity was yesterday's after-hours: not premarket volume.
        "NVDA": _quote(
            session="extended",
            volume=10_000,
            event_timestamp=dt.datetime(2026, 9, 25, 19, 30, tzinfo=EASTERN),
        ),
        # Traded today but before the configured premarket start.
        "AMD": _quote(
            session="extended",
            volume=10_000,
            event_timestamp=dt.datetime(2026, 9, 28, 3, 30, tzinfo=EASTERN),
        ),
        "INTC": _quote(session="regular", volume=10_000, event_timestamp=None),
    }

    assert symbols_needing_rvol_fetch(
        quotes, in_premarket=True, generated_at=PREMARKET_AT
    ) == ["AAPL"]


def test_symbols_needing_rvol_fetch_returns_nothing_outside_premarket() -> None:
    quotes = {
        "AAPL": _quote(session="extended", volume=10_000, event_timestamp=PREMARKET_AT)
    }

    assert (
        symbols_needing_rvol_fetch(quotes, in_premarket=False, generated_at=PREMARKET_AT)
        == []
    )


async def test_fetch_avg_volumes_uses_provider_per_symbol() -> None:
    provider = FakeProvider(
        {
            "AAPL": [
                {"datetime": _ms(2), "close": 100.0, "volume": 1_000_000},
                {"datetime": _ms(1), "close": 101.0, "volume": 1_200_000},
            ],
            "MSFT": [],
        }
    )

    result = await fetch_avg_volumes(provider, ["AAPL", "MSFT"], concurrency=2, lookback_days=2)

    assert result == {"AAPL": (1_000_000 + 1_200_000) / 2}
    assert sorted(provider.calls) == [("AAPL", 2), ("MSFT", 2)]


async def test_fetch_prior_day_changes_uses_provider_per_symbol() -> None:
    provider = FakeProvider(
        {
            "AAPL": [
                {"datetime": _ms(2), "close": 100.0, "volume": 1_000_000},
                {"datetime": _ms(1), "close": 103.0, "volume": 1_000_000},
            ],
        }
    )

    result = await fetch_prior_day_changes(provider, ["AAPL"], days_back=30)

    assert result == {"AAPL": 3.0}
    assert provider.calls == [("AAPL", 30)]
