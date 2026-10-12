import datetime as dt

import pytest

from equity_scanner.time_utils import (
    EARLY_CLOSE,
    MARKET_CLOSE,
    get_us_market_early_closes,
    is_trading_day,
    market_close_time,
)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        # New Year's Day on Saturday is not observed on the prior Friday.
        ("2021-12-31", True),
        ("2027-12-31", True),
        ("2022-01-03", True),
        # New Year's Day on Sunday is observed on Monday.
        ("2023-01-02", False),
        ("2026-01-01", False),
        ("2026-04-03", False),  # Good Friday
        ("2026-06-19", False),
        ("2026-07-03", False),  # Independence Day observed
        ("2026-10-12", True),  # Columbus Day: NYSE open
        ("2027-12-24", False),  # Christmas observed
    ],
)
def test_is_trading_day_follows_nyse_calendar(day, expected):
    assert is_trading_day(dt.date.fromisoformat(day)) is expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2023, {"2023-07-03", "2023-11-24"}),
        (2024, {"2024-07-03", "2024-11-29", "2024-12-24"}),
        (2025, {"2025-07-03", "2025-11-28", "2025-12-24"}),
        (2026, {"2026-11-27", "2026-12-24"}),
        (2027, {"2027-11-26"}),
    ],
)
def test_early_closes_are_computed_for_any_year(year, expected):
    assert get_us_market_early_closes(year) == {dt.date.fromisoformat(d) for d in expected}


def test_market_close_time_uses_early_close():
    assert market_close_time(dt.date(2027, 11, 26)) == EARLY_CLOSE
    assert market_close_time(dt.date(2027, 11, 24)) == MARKET_CLOSE
