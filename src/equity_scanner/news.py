"""Free-source catalyst signals for the equity morning scan, ported from
ButterflyGuy's equity_scan/news.py. Zero Schwab/gateway dependency — this is SEC
EDGAR full-text search + company-facts lookups, and Alpha Vantage's news/earnings
APIs, keyed by ticker. Uses stdlib `logging` instead of ButterflyGuy's structlog
(equity-scanner has no structlog dependency and doesn't need one for this)."""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import logging
import os
from dataclasses import dataclass
from io import StringIO
from typing import Any

import httpx

from equity_scanner.scan_config import EquityNewsSettings
from equity_scanner.time_utils import EASTERN

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsImpact:
    symbol: str
    score: float
    reasons: tuple[str, ...] = ()
    recent_headlines: tuple[str, ...] = ()
    upcoming_events: tuple[str, ...] = ()
    sec_forms: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()


def _today(generated_at: dt.datetime | None) -> dt.date:
    ts = generated_at or dt.datetime.now(EASTERN)
    return ts.astimezone(EASTERN).date()


def _parse_date(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _merge_impact(
    existing: NewsImpact | None,
    *,
    symbol: str,
    score: float,
    reasons: list[str] | None = None,
    headlines: list[str] | None = None,
    events: list[str] | None = None,
    forms: list[str] | None = None,
    providers: list[str] | None = None,
) -> NewsImpact:
    if existing is None:
        existing = NewsImpact(symbol=symbol, score=0.0)

    return NewsImpact(
        symbol=symbol,
        score=existing.score + score,
        reasons=tuple(dict.fromkeys((*existing.reasons, *(reasons or [])))),
        recent_headlines=tuple(
            dict.fromkeys((*existing.recent_headlines, *(headlines or [])))
        )[:3],
        upcoming_events=tuple(dict.fromkeys((*existing.upcoming_events, *(events or []))))[:3],
        sec_forms=tuple(dict.fromkeys((*existing.sec_forms, *(forms or []))))[:5],
        providers=tuple(dict.fromkeys((*existing.providers, *(providers or [])))),
    )


def _sec_form_score(form: str) -> float:
    if form == "8-K":
        return 6.0
    if form in {"SC 13D", "SC 13G"}:
        return 5.0
    if form in {"10-Q", "10-K", "S-1", "S-3"}:
        return 4.0
    if form in {"DEF 14A", "PRE 14A"}:
        return 3.0
    return 2.0


def _select_news_symbols(symbols: list[str], limit: int) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = symbol.upper().replace(".", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
        if len(selected) >= limit:
            break
    return selected


async def _fetch_json(client: httpx.AsyncClient, url: str, **params: Any) -> Any:
    response = await client.get(url, params=params or None)
    response.raise_for_status()
    return response.json()


async def _load_sec_ticker_map(client: httpx.AsyncClient) -> dict[str, int]:
    payload = await _fetch_json(client, SEC_TICKERS_URL)
    ticker_map: dict[str, int] = {}
    for item in payload.values():
        ticker = str(item.get("ticker", "")).upper()
        cik = item.get("cik_str")
        if ticker and cik is not None:
            ticker_map[ticker] = int(cik)
    return ticker_map


def _recent_sec_filings(
    symbol: str,
    payload: dict[str, Any],
    *,
    today: dt.date,
    settings: EquityNewsSettings,
) -> NewsImpact | None:
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    descriptions = recent.get("primaryDocDescription") or []
    allowed_forms = set(settings.sec_forms)

    impact: NewsImpact | None = None
    for idx, form in enumerate(forms):
        form = str(form)
        if form not in allowed_forms:
            continue
        filing_date = _parse_date(dates[idx] if idx < len(dates) else None)
        if filing_date is None or (today - filing_date).days > settings.recent_days:
            continue
        description = str(descriptions[idx] if idx < len(descriptions) else "").strip()
        label = f"{form} filed {filing_date.isoformat()}"
        if description and description != form:
            label = f"{label}: {description[:80]}"
        impact = _merge_impact(
            impact,
            symbol=symbol,
            score=_sec_form_score(form),
            reasons=["recent SEC filing"],
            headlines=[label],
            forms=[form],
            providers=["sec"],
        )
    return impact


async def _fetch_sec_impacts(
    symbols: list[str],
    *,
    settings: EquityNewsSettings,
    generated_at: dt.datetime | None,
) -> dict[str, NewsImpact]:
    if "sec" not in settings.providers:
        return {}

    headers = {
        "User-Agent": os.getenv(settings.sec_user_agent_env, settings.sec_user_agent),
        "Accept-Encoding": "gzip, deflate",
    }
    today = _today(generated_at)
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    impacts: dict[str, NewsImpact] = {}

    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        try:
            ticker_map = await _load_sec_ticker_map(client)
        except Exception as exc:
            log.warning("sec_ticker_map_fetch_failed error=%s", exc)
            return {}
        for symbol in symbols:
            cik = ticker_map.get(symbol)
            if cik is None:
                continue
            try:
                payload = await _fetch_json(client, SEC_SUBMISSIONS_URL.format(cik=cik))
            except Exception as exc:
                log.warning("sec_news_fetch_failed symbol=%s error=%s", symbol, exc)
                continue
            impact = _recent_sec_filings(
                symbol,
                payload,
                today=today,
                settings=settings,
            )
            if impact is not None:
                impacts[symbol] = impact
            await asyncio.sleep(0.1)
    return impacts


def _alpha_key(settings: EquityNewsSettings) -> str | None:
    key = os.getenv(settings.alpha_vantage_api_key_env)
    return key.strip() if key and key.strip() else None


async def _fetch_alpha_earnings(
    client: httpx.AsyncClient,
    *,
    symbols: set[str],
    settings: EquityNewsSettings,
    api_key: str,
    generated_at: dt.datetime | None,
) -> dict[str, NewsImpact]:
    today = _today(generated_at)
    upcoming_cutoff = today + dt.timedelta(days=settings.upcoming_days)
    response = await client.get(
        ALPHA_VANTAGE_URL,
        params={
            "function": "EARNINGS_CALENDAR",
            "horizon": "3month",
            "apikey": api_key,
        },
    )
    response.raise_for_status()

    impacts: dict[str, NewsImpact] = {}
    for row in csv.DictReader(StringIO(response.text)):
        symbol = (row.get("symbol") or "").upper()
        if symbol not in symbols:
            continue
        report_date = _parse_date(row.get("reportDate"))
        if report_date is None or not today <= report_date <= upcoming_cutoff:
            continue
        event = f"earnings expected {report_date.isoformat()}"
        fiscal = row.get("fiscalDateEnding")
        if fiscal:
            event = f"{event} for quarter ending {fiscal}"
        impacts[symbol] = _merge_impact(
            impacts.get(symbol),
            symbol=symbol,
            score=5.0,
            reasons=["upcoming earnings"],
            events=[event],
            providers=["alpha_vantage"],
        )
    return impacts


async def _fetch_alpha_news_for_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    settings: EquityNewsSettings,
    api_key: str,
) -> NewsImpact | None:
    response = await client.get(
        ALPHA_VANTAGE_URL,
        params={
            "function": "NEWS_SENTIMENT",
            "tickers": symbol,
            "sort": "RELEVANCE",
            "limit": settings.alpha_vantage_news_limit,
            "apikey": api_key,
        },
    )
    response.raise_for_status()
    payload = response.json()
    feed = payload.get("feed") or []

    impact: NewsImpact | None = None
    for article in feed[: settings.alpha_vantage_news_limit]:
        title = str(article.get("title") or "").strip()
        topics = {
            str(topic.get("topic", "")).lower()
            for topic in article.get("topics", [])
            if isinstance(topic, dict)
        }
        ticker_sentiment = article.get("ticker_sentiment") or []
        relevance = 0.0
        for item in ticker_sentiment:
            if str(item.get("ticker", "")).upper() == symbol:
                try:
                    relevance = max(relevance, float(item.get("relevance_score") or 0.0))
                except (TypeError, ValueError):
                    pass
        if relevance < 0.2:
            continue

        score = 1.0 + min(relevance, 1.0) * 2.0
        reasons = ["recent headline"]
        if {"earnings", "mergers & acquisitions", "ipo"} & topics:
            score += 2.0
            reasons.append("impact topic")
        impact = _merge_impact(
            impact,
            symbol=symbol,
            score=score,
            reasons=reasons,
            headlines=[title] if title else None,
            providers=["alpha_vantage"],
        )
    return impact


async def _fetch_alpha_impacts(
    symbols: list[str],
    *,
    settings: EquityNewsSettings,
    generated_at: dt.datetime | None,
) -> dict[str, NewsImpact]:
    if "alpha_vantage" not in settings.providers:
        return {}
    api_key = _alpha_key(settings)
    if api_key is None:
        log.info("alpha_vantage_news_skipped reason=missing_api_key")
        return {}

    selected = symbols[: settings.alpha_vantage_max_news_symbols]
    timeout = httpx.Timeout(settings.request_timeout_seconds)
    impacts: dict[str, NewsImpact] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            impacts.update(
                await _fetch_alpha_earnings(
                    client,
                    symbols=set(symbols),
                    settings=settings,
                    api_key=api_key,
                    generated_at=generated_at,
                )
            )
        except Exception as exc:
            log.warning("alpha_vantage_earnings_failed error=%s", exc)

        for symbol in selected:
            try:
                impact = await _fetch_alpha_news_for_symbol(
                    client,
                    symbol,
                    settings=settings,
                    api_key=api_key,
                )
            except Exception as exc:
                log.warning("alpha_vantage_news_failed symbol=%s error=%s", symbol, exc)
                continue
            if impact is not None:
                impacts[symbol] = _merge_impact(
                    impacts.get(symbol),
                    symbol=symbol,
                    score=impact.score,
                    reasons=list(impact.reasons),
                    headlines=list(impact.recent_headlines),
                    events=list(impact.upcoming_events),
                    forms=list(impact.sec_forms),
                    providers=list(impact.providers),
                )
    return impacts


def merge_news_impacts(*impact_maps: dict[str, NewsImpact]) -> dict[str, NewsImpact]:
    merged: dict[str, NewsImpact] = {}
    for impact_map in impact_maps:
        for symbol, impact in impact_map.items():
            merged[symbol] = _merge_impact(
                merged.get(symbol),
                symbol=symbol,
                score=impact.score,
                reasons=list(impact.reasons),
                headlines=list(impact.recent_headlines),
                events=list(impact.upcoming_events),
                forms=list(impact.sec_forms),
                providers=list(impact.providers),
            )
    return merged


async def fetch_news_impacts(
    symbols: list[str],
    *,
    settings: EquityNewsSettings,
    generated_at: dt.datetime | None = None,
) -> dict[str, NewsImpact]:
    """Fetch recent/coming catalyst signals from free data sources."""
    if not settings.enabled:
        return {}

    selected = _select_news_symbols(symbols, settings.max_symbols)
    if not selected:
        return {}

    sec_impacts, alpha_impacts = await asyncio.gather(
        _fetch_sec_impacts(selected, settings=settings, generated_at=generated_at),
        _fetch_alpha_impacts(selected, settings=settings, generated_at=generated_at),
        return_exceptions=True,
    )
    if isinstance(sec_impacts, Exception):
        log.warning("sec_news_provider_failed error=%s", sec_impacts)
        sec_impacts = {}
    if isinstance(alpha_impacts, Exception):
        log.warning("alpha_vantage_provider_failed error=%s", alpha_impacts)
        alpha_impacts = {}
    return merge_news_impacts(sec_impacts, alpha_impacts)
