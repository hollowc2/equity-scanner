from __future__ import annotations

import datetime as dt
import json

from schwab_gateway_sdk import QuoteV1

from equity_scanner.closes import (
    load_previous_closes,
    previous_trading_day,
    prior_day_changes_from_closes,
    record_closes,
)
from equity_scanner.time_utils import EASTERN


def _quote(symbol: str, close: float | None) -> QuoteV1:
    return QuoteV1(
        symbol=symbol,
        gateway_received_at=dt.datetime.now(dt.timezone.utc),
        source="test",
        close=close,
        stale=False,
    )


def test_previous_trading_day_skips_weekends_and_holidays():
    assert previous_trading_day(dt.date(2026, 9, 28)) == dt.date(2026, 9, 25)  # Mon -> Fri
    assert previous_trading_day(dt.date(2026, 9, 8)) == dt.date(2026, 9, 4)  # Labor Day
    assert previous_trading_day(dt.date(2026, 9, 24)) == dt.date(2026, 9, 23)


def test_morning_closes_give_yesterdays_change_on_the_next_trading_day(tmp_path):
    friday_morning = dt.datetime(2026, 9, 25, 9, 0, tzinfo=EASTERN)
    monday_morning = dt.datetime(2026, 9, 28, 9, 0, tzinfo=EASTERN)

    # Friday's pre-open quotes carry Thursday's closes.
    path = record_closes(
        {"AAPL": _quote("AAPL", 100.0), "MSFT": _quote("MSFT", 50.0), "BAD": _quote("BAD", None)},
        closes_dir=tmp_path,
        generated_at=friday_morning,
    )
    payload = json.loads(path.read_text())
    assert path.name == "2026-09-25.json"
    assert payload["session_closed"] == "2026-09-24"
    assert payload["closes"] == {"AAPL": 100.0, "MSFT": 50.0}

    # Monday's quotes carry Friday's closes; the change is Friday's session move.
    monday_quotes = {"AAPL": _quote("AAPL", 108.0), "NEW": _quote("NEW", 10.0)}
    previous = load_previous_closes(tmp_path, today=monday_morning.date())
    changes = prior_day_changes_from_closes(monday_quotes, previous)

    assert changes == {"AAPL": 8.0}


def test_closes_are_not_recorded_after_the_open_or_on_non_trading_days(tmp_path):
    quotes = {"AAPL": _quote("AAPL", 100.0)}

    after_open = dt.datetime(2026, 9, 25, 12, 45, tzinfo=EASTERN)
    saturday = dt.datetime(2026, 9, 26, 9, 0, tzinfo=EASTERN)

    assert record_closes(quotes, closes_dir=tmp_path, generated_at=after_open) is None
    assert record_closes(quotes, closes_dir=tmp_path, generated_at=saturday) is None
    assert list(tmp_path.iterdir()) == []


def test_missing_or_corrupt_previous_closes_yield_no_changes(tmp_path):
    today = dt.date(2026, 9, 28)
    assert load_previous_closes(tmp_path, today=today) == {}

    (tmp_path / "2026-09-25.json").write_text("{not json")
    assert load_previous_closes(tmp_path, today=today) == {}
