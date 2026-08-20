"""Tests for the ported free-source catalyst parsing (news.py). Mirrors ButterflyGuy's
own test_equity_scan_news.py: only the pure scoring/parsing helpers are exercised —
the httpx-network-calling orchestrators (_fetch_sec_impacts/_fetch_alpha_impacts/
fetch_news_impacts) aren't unit tested upstream either, since they're thin wrappers
around live SEC/Alpha Vantage HTTP calls with no meaningful logic of their own beyond
what's already covered here."""

from __future__ import annotations

import datetime as dt

from equity_scanner.news import (
    NewsImpact,
    _merge_impact,
    _recent_sec_filings,
    _sec_form_score,
    _select_news_symbols,
    merge_news_impacts,
)
from equity_scanner.scan_config import EquityNewsSettings
from equity_scanner.time_utils import EASTERN


def test_sec_form_score_ranks_8k_highest():
    assert _sec_form_score("8-K") == 6.0
    assert _sec_form_score("SC 13D") == 5.0
    assert _sec_form_score("10-Q") == 4.0
    assert _sec_form_score("DEF 14A") == 3.0
    assert _sec_form_score("4") == 2.0  # unlisted form falls back to the default score


def test_select_news_symbols_dedupes_and_normalizes_dots_and_limits():
    selected = _select_news_symbols(["aapl", "BRK.A", "brk.a", "msft", "goog"], limit=3)
    assert selected == ["AAPL", "BRK-A", "MSFT"]


def test_recent_sec_filings_scores_impact_forms():
    settings = EquityNewsSettings(recent_days=3)
    today = dt.datetime(2026, 6, 8, 8, 0, tzinfo=EASTERN).date()
    impact = _recent_sec_filings(
        "AAPL",
        {
            "filings": {
                "recent": {
                    "form": ["8-K", "4", "10-Q"],
                    "filingDate": ["2026-06-07", "2026-06-07", "2026-06-01"],
                    "primaryDocDescription": ["Current report", "", "Quarterly report"],
                }
            }
        },
        today=today,
        settings=settings,
    )
    assert impact is not None
    assert impact.symbol == "AAPL"
    assert impact.score == 6.0
    assert impact.reasons == ("recent SEC filing",)
    assert impact.sec_forms == ("8-K",)
    assert impact.providers == ("sec",)
    assert impact.recent_headlines == ("8-K filed 2026-06-07: Current report",)


def test_recent_sec_filings_ignores_old_and_disallowed_forms():
    settings = EquityNewsSettings(recent_days=1)
    today = dt.datetime(2026, 6, 8, 8, 0, tzinfo=EASTERN).date()
    impact = _recent_sec_filings(
        "AAPL",
        {
            "filings": {
                "recent": {
                    "form": ["8-K", "4"],
                    "filingDate": ["2026-06-01", "2026-06-08"],  # 8-K is too old (7 days)
                }
            }
        },
        today=today,
        settings=settings,
    )
    assert impact is None  # "4" isn't an allowed form; the 8-K is outside recent_days


def test_merge_impact_accumulates_score_and_dedupes_reasons():
    first = _merge_impact(
        None, symbol="AAPL", score=6.0, reasons=["recent SEC filing"], headlines=["8-K filed"]
    )
    merged = _merge_impact(
        first, symbol="AAPL", score=5.0, reasons=["recent SEC filing", "upcoming earnings"]
    )
    assert merged.score == 11.0
    assert merged.reasons == ("recent SEC filing", "upcoming earnings")
    assert merged.recent_headlines == ("8-K filed",)


def test_merge_news_impacts_dedupes_context():
    merged = merge_news_impacts(
        {
            "AAPL": NewsImpact(
                symbol="AAPL",
                score=6.0,
                reasons=("recent SEC filing",),
                recent_headlines=("8-K filed 2026-06-07",),
                providers=("sec",),
            )
        },
        {
            "AAPL": NewsImpact(
                symbol="AAPL",
                score=5.0,
                reasons=("upcoming earnings",),
                upcoming_events=("earnings expected 2026-06-10",),
                providers=("alpha_vantage",),
            )
        },
    )
    impact = merged["AAPL"]
    assert impact.score == 11.0
    assert impact.reasons == ("recent SEC filing", "upcoming earnings")
    assert impact.providers == ("sec", "alpha_vantage")
