"""Check candidate coverage and signal timing with synthetic gateway candles."""

import datetime as dt

import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner.closes import previous_trading_day
from equity_scanner.provider import StaleGatewayDataError
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import EquitySnapshot
from equity_scanner.time_utils import EASTERN
from equity_scanner.trade_setups import (
    _capitulation,
    _completed_daily,
    _daily_dip,
    _opening_tape,
    _regular_minutes,
    _tape_quote,
    _two_minute_bars,
    scan_trade_setups,
)

OPEN = dt.datetime(2026, 11, 20, 9, 30, tzinfo=EASTERN)
MINUTE = dt.timedelta(minutes=1)


def _candle(start, *, high=101, low=99, close=100, open_=100, volume=100):
    return {
        "datetime": int(start.timestamp() * 1000),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _daily_candles(last_day):
    days = [last_day]
    for _ in range(17):
        days.append(previous_trading_day(days[-1]))
    candles = [
        _candle(dt.datetime.combine(day, dt.time(1), tzinfo=EASTERN)) for day in reversed(days)
    ]
    candles[-1].update(high=96, low=91, open=95, close=92, volume=280)
    return candles


def _minutes(count):
    return [_candle(OPEN + i * MINUTE) for i in range(count)]


def _flush_minutes():
    bars = _minutes(20)
    bars.extend(
        [
            _candle(OPEN + 20 * MINUTE, high=98, low=93, close=94, open_=97, volume=330),
            _candle(OPEN + 21 * MINUTE, high=97, low=93.5, close=96, open_=94, volume=270),
            _candle(OPEN + 22 * MINUTE, high=99, low=95, close=98, open_=96),
            _candle(OPEN + 23 * MINUTE, high=100, low=97, close=99, open_=98),
        ]
    )
    return bars


def test_daily_selloff_uses_completed_dates_and_requires_low_to_hold():
    before = previous_trading_day(OPEN.date())
    candles = _daily_candles(before)
    current = _candle(OPEN, high=100, low=92, close=99, open_=95)
    candles.append(current)

    watch = _daily_dip("TEST", _completed_daily(candles, before))
    assert watch is not None
    assert watch.status == "Selloff watch"
    assert watch.change_pct == -8.0
    assert watch.atr_multiple == 4.5  # Overnight gap is included in true range.
    assert watch.volume_multiple == 2.8
    assert watch.observed_date == before.isoformat()

    follow = _daily_dip("TEST", _completed_daily(candles, OPEN.date()))
    assert follow is not None
    assert follow.status == "Follow-through"
    assert (follow.selloff_low, follow.selloff_high) == (91, 96)
    assert follow.observed_date == OPEN.date().isoformat()

    current["low"] = 90
    assert _daily_dip("TEST", _completed_daily(candles, OPEN.date())) is None


def test_two_minute_signal_waits_for_both_minute_bars_and_disallows_a_failed_reclaim():
    candles = _flush_minutes()
    during = OPEN + 23 * MINUTE + dt.timedelta(seconds=45)
    watch = _capitulation("TEST", _two_minute_bars(_regular_minutes(candles, OPEN, during)), 100)
    assert watch is not None
    assert watch.status == "Flush watch"
    assert watch.flush_end == (OPEN + 22 * MINUTE).isoformat()

    cutoff = OPEN + 24 * MINUTE
    bars = _two_minute_bars(_regular_minutes(candles, OPEN, cutoff))
    signal = _capitulation("TEST", bars, 100)
    assert signal is not None
    assert signal.status == "Reclaim confirmed"
    assert signal.observed_end == cutoff.isoformat()
    assert signal.volume_multiple == 3
    assert signal.low_vs_prior_close_pct == -7

    candles.extend(
        [
            _candle(OPEN + 24 * MINUTE, high=99, low=94, close=96, open_=98),
            _candle(OPEN + 25 * MINUTE, high=98, low=96, close=97, open_=96),
        ]
    )
    failed = _two_minute_bars(_regular_minutes(candles, OPEN, OPEN + 26 * MINUTE))
    assert _capitulation("TEST", failed, 100) is None

    # A missing interior minute prevents both a session low and a session VWAP claim.
    candles.pop(10)
    assert _regular_minutes(candles, OPEN, cutoff) == []


def test_opening_range_break_is_based_on_a_closed_minute_outside_range_and_vwap():
    candles = _minutes(15)
    candles.append(_candle(OPEN + 15 * MINUTE, high=103, low=100, close=102))
    candles.append(_candle(OPEN + 16 * MINUTE, high=104, low=101, close=103, open_=102))
    pending = _regular_minutes(candles, OPEN, OPEN + 15 * MINUTE)
    assert _opening_tape(pending) is None
    new_break = _opening_tape(_regular_minutes(candles, OPEN, OPEN + 16 * MINUTE))
    assert new_break is not None
    assert new_break.direction == "New upside break"
    assert (new_break.opening_low, new_break.opening_high) == (99, 101)
    assert 100 < new_break.vwap < new_break.close
    holding = _opening_tape(_regular_minutes(candles, OPEN, OPEN + 17 * MINUTE))
    assert holding is not None
    assert holding.direction == "Upside break holding"


def test_premarket_tape_labels_a_prior_session_print():
    prior = previous_trading_day(OPEN.date())
    stamp = dt.datetime.combine(prior, dt.time(16, 15), tzinfo=EASTERN)
    quote = QuoteV1(
        symbol="$VIX",
        event_timestamp=stamp,
        gateway_received_at=stamp,
        source="test",
        session="regular",
        last=22,
        close=20,
        stale=True,
    )

    reference = _tape_quote(quote, OPEN - MINUTE)
    assert reference is not None and reference.prior_session
    assert reference.observed_at == stamp.isoformat()
    assert reference.change_pct == 10
    assert _tape_quote(quote, OPEN + MINUTE) is None


@pytest.mark.asyncio
async def test_scan_caps_history_work_and_surfaces_missing_coverage():
    cutoff = OPEN + 24 * MINUTE
    calls = []

    class Provider:
        async def get_daily_bars(self, symbol, days_back):
            calls.append((symbol, days_back))
            return _daily_candles(previous_trading_day(cutoff.date()))

        async def get_minute_bars(self, symbol):
            calls.append((symbol, "minute"))
            if symbol == "SPY":
                raise StaleGatewayDataError("test stale history")
            return _flush_minutes()

    def quote(symbol, price, *, age=0):
        return QuoteV1(
            symbol=symbol,
            session="regular",
            event_timestamp=cutoff - age * MINUTE,
            gateway_received_at=cutoff,
            source="test",
            last=price,
            close=100 if not symbol.startswith("$") else None,
            stale=False,
        )

    def snapshot(symbol, change):
        return EquitySnapshot(
            symbol=symbol,
            price=100 + change,
            prior_close=100,
            prior_day_pct=None,
            session_gap_pct=change,
            volume=1_000_000,
            premarket_volume=0,
            avg_volume_20d=1_000_000,
            rvol=None,
            sector="Other",
            universes=("liquid",),
        )

    snapshots = [snapshot("A", -4), snapshot("B", -2)]
    result = await scan_trade_setups(
        Provider(),
        snapshots,
        quotes={"A": quote("A", 96), "B": quote("B", 98)},
        context_quotes={"$SPX": quote("$SPX", 6500), "$VIX": quote("$VIX", 17, age=6)},
        sp500_symbols=["A", "B", "MISSING"],
        known_prior_day={},
        settings=EquityScanSettings(
            trade_setups={"daily_candidates": 1, "intraday_candidates": 1},
        ),
        as_of=cutoff,
    )
    assert len(calls) == 3
    assert set(calls) == {("A", 20), ("A", "minute"), ("SPY", "minute")}
    assert result.spx is not None and result.spx.level == 6500
    assert result.vix is None
    assert result.spy_tape is None and result.spy_status == "minute coverage unavailable"
    assert result.breadth is not None
    assert (result.breadth.advances, result.breadth.declines, result.breadth.total) == (0, 2, 3)
    assert result.daily_coverage.evaluated == result.intraday_coverage.evaluated == 1
    assert [dip.symbol for dip in result.daily_dips] == ["A"]
    assert [signal.symbol for signal in result.capitulations] == ["A"]
