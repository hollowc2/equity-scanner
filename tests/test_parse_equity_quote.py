"""Focused unit tests for parse_equity_quote/_price_choice's session-aware behavior —
regression coverage for two /code-review fixes: premarket_volume must not count
after-hours activity as premarket, and _price_choice must not treat a bad negative
price as valid data."""

from __future__ import annotations

import datetime as dt

from schwab_gateway_sdk import QuoteV1

from equity_scanner.scanner import _price_choice, parse_equity_quote


def _quote(
    *,
    session: str | None,
    last: float | None = 100.0,
    mark: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    close: float | None = 90.0,
    volume: int | None = 1_000_000,
    net_percent_change: float | None = None,
) -> QuoteV1:
    return QuoteV1(
        symbol="TEST",
        gateway_received_at=dt.datetime.now(dt.timezone.utc),
        source="test",
        session=session,
        last=last,
        mark=mark,
        bid=bid,
        ask=ask,
        close=close,
        volume=volume,
        net_percent_change=net_percent_change,
        stale=False,
    )


def test_premarket_volume_is_zero_outside_the_premarket_window_even_with_extended_session():
    """The gateway reports session="extended" after the 4pm close just as much as
    before the 9:30am open; without in_premarket gating this data would be
    mislabeled as a premarket rvol/gap signal."""
    quote = _quote(session="extended", volume=500_000)

    snapshot = parse_equity_quote("AAPL", quote, universes={"sp500"}, in_premarket=False)

    assert snapshot is not None
    assert snapshot.premarket_volume == 0
    assert snapshot.rvol is None


def test_premarket_volume_counts_extended_volume_during_the_premarket_window():
    quote = _quote(session="extended", volume=500_000)

    snapshot = parse_equity_quote(
        "AAPL", quote, universes={"sp500"}, in_premarket=True, avg_volume_20d=1_000_000
    )

    assert snapshot is not None
    assert snapshot.premarket_volume == 500_000
    assert snapshot.rvol == 0.5


def test_price_choice_rejects_negative_last_and_falls_through_to_mark():
    quote = _quote(session="regular", last=-5.0, mark=101.0)

    price, source, _ = _price_choice(quote)

    assert price == 101.0
    assert source == "regular.mark"


def test_price_choice_rejects_zero_last_and_falls_through_to_bid_ask_mid():
    quote = _quote(session="regular", last=0.0, mark=None, bid=99.0, ask=101.0)

    price, source, _ = _price_choice(quote)

    assert price == 100.0
    assert source == "regular.bid_ask_mid"


def test_missing_regular_net_percent_uses_selected_price_against_regular_close():
    quote = _quote(session="extended", last=99.0, close=90.0, net_percent_change=None)

    snapshot = parse_equity_quote("AAPL", quote, universes={"sp500"})

    assert snapshot is not None
    assert snapshot.prior_day_pct == 10.0


def test_percent_disagreement_gate_is_unavailable_after_extended_session_is_selected():
    extended = _quote(
        session="extended", last=99.0, close=90.0, net_percent_change=-20.0
    )
    regular = extended.model_copy(update={"session": "regular"})

    assert (
        parse_equity_quote(
            "AAPL",
            extended,
            universes={"sp500"},
            max_price_disagreement_pct=5.0,
        )
        is not None
    )
    assert (
        parse_equity_quote(
            "AAPL",
            regular,
            universes={"sp500"},
            max_price_disagreement_pct=5.0,
        )
        is None
    )
