import datetime as dt

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

    async def get_daily_bars(self, symbol: str, days_back: int | None = None) -> list[dict]:
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


def test_symbols_needing_rvol_fetch_only_includes_premarket_volume() -> None:
    quotes = {
        "AAPL": {"extended": {"totalVolume": 10_000}},
        "MSFT": {"extended": {"totalVolume": 0}},
        "NVDA": {"extended": {}},
    }

    assert symbols_needing_rvol_fetch(quotes) == ["AAPL"]


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


async def test_fetch_prior_day_changes_uses_provider_per_symbol() -> None:
    provider = FakeProvider(
        {
            "AAPL": [
                {"datetime": _ms(2), "close": 100.0, "volume": 1_000_000},
                {"datetime": _ms(1), "close": 103.0, "volume": 1_000_000},
            ],
        }
    )

    result = await fetch_prior_day_changes(provider, ["AAPL"])

    assert result == {"AAPL": 3.0}
