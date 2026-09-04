"""Morning equity scan orchestration — CLI entry point (`equity-scanner-run`), ported
from ButterflyGuy's scripts/run_morning_scan.py. Wires universes -> quotes -> volume
-> snapshots -> ranking -> news -> report -> archive -> Discord end to end.

Keeps the original's two-pass structure: a preliminary rank on the closing-price-
derived `prior_day_pct` decides which symbols are worth a `fetch_prior_day_changes`
history refetch (opening-focus/gainers/losers candidates, not the whole universe —
that's a deliberate cost-control choice, not an accident), then re-ranks with the
more accurate prior-day change attached."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import asdict, replace

from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.news import fetch_news_impacts
from equity_scanner.notifier import DiscordNotifier
from equity_scanner.provider import GatewayEquityDataProvider, QuoteCoverageMode
from equity_scanner.report import archive_report, archive_report_json, build_report
from equity_scanner.scan_config import load_equity_scan_config
from equity_scanner.scanner import (
    EquitySnapshot,
    ScanResults,
    attach_news_impacts,
    build_snapshots,
    parse_market_context,
    rank_scan_results,
)
from equity_scanner.time_utils import is_premarket_window, is_trading_day, now_eastern
from equity_scanner.universes import (
    build_symbol_map,
    load_liquid_meta,
    load_sector_map,
    load_universes,
)
from equity_scanner.volume import (
    fetch_avg_volumes,
    fetch_prior_day_changes,
    symbols_needing_rvol_fetch,
)

log = logging.getLogger("equity_scanner.run")


def _record_phase(
    timings: dict[str, float],
    phase: str,
    started_at: float,
) -> float:
    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
    timings[phase] = elapsed_ms
    log.info("equity_scan_phase phase=%s elapsed_ms=%.3f", phase, elapsed_ms)
    return elapsed_ms


def _news_candidate_symbols(snapshots: list[EquitySnapshot], *, limit: int) -> list[str]:
    ordered = sorted(
        snapshots,
        key=lambda snap: (
            "custom" in snap.universes,
            abs(snap.session_gap_pct),
            abs(snap.prior_day_pct),
            snap.volume,
        ),
        reverse=True,
    )
    return [snapshot.symbol for snapshot in ordered[:limit]]


def _prior_day_change_symbols(results: ScanResults) -> list[str]:
    symbols: set[str] = set()
    symbols.update(item.snapshot.symbol for item in results.opening_focus)
    symbols.update(snapshot.symbol for snapshot in results.prior_gainers)
    symbols.update(snapshot.symbol for snapshot in results.prior_losers)
    symbols.update(snapshot.symbol for snapshot in results.premarket_gainers)
    symbols.update(snapshot.symbol for snapshot in results.premarket_losers)
    return sorted(symbols)


async def run_scan(
    *,
    scan_config_path: str = "configs/equity_scan.yaml",
    dry_run: bool = False,
    open_scan: bool = False,
    quote_coverage_mode: QuoteCoverageMode = "strict",
) -> list[str]:
    if quote_coverage_mode != "strict" and not dry_run:
        raise ValueError(f"{quote_coverage_mode} quote coverage requires --dry-run")
    generated_at = now_eastern()
    if not is_trading_day(generated_at.date()):
        log.info("equity_scan_skipped reason=not_trading_day date=%s", generated_at.date())
        return []

    run_started = time.perf_counter()
    phase_timings_ms: dict[str, float] = {}
    phase_started = time.perf_counter()
    app_settings = AppSettings()
    scan_config = load_equity_scan_config(scan_config_path)
    if app_settings.sec_user_agent:
        os.environ.setdefault(scan_config.news.sec_user_agent_env, app_settings.sec_user_agent)
    if app_settings.alpha_vantage_api_key:
        os.environ.setdefault(
            scan_config.news.alpha_vantage_api_key_env,
            app_settings.alpha_vantage_api_key.get_secret_value(),
        )
    if open_scan:
        scan_config.include_movers = True

    universes = load_universes(
        scan_config.universes,
        universe_dir=scan_config.universe_dir,
        custom_watchlist=scan_config.custom_watchlist,
    )
    symbol_map = build_symbol_map(universes)
    symbols = sorted(symbol_map)
    if not symbols:
        raise RuntimeError(
            "No symbols loaded. Refresh universes with `equity-scanner-refresh-universes` "
            "and add tickers to custom.txt."
        )
    _record_phase(phase_timings_ms, "setup", phase_started)

    async with build_gateway_client(app_settings) as gateway:
        provider = GatewayEquityDataProvider(
            gateway,
            max_attempts=app_settings.gateway_max_attempts,
            retry_backoff_seconds=app_settings.gateway_retry_backoff_seconds,
        )
        log.info(
            "equity_scan_start universes=%s symbols=%d",
            scan_config.universes,
            len(symbols),
        )
        phase_started = time.perf_counter()
        paced = quote_coverage_mode == "parity-paced-recovery"
        quote_collection = await provider.get_equity_quote_collection(
            symbols,
            batch_size=scan_config.batch_size,
            concurrency=1 if paced else 4,
            mode=quote_coverage_mode,
            # A gateway outage is usually transient; this app has no hard deadline
            # of its own beyond finishing before the downstream alert send, so it
            # is worth waiting out a few backed-off retries per failed batch
            # rather than giving up after one.
            max_recovery_attempts=4 if paced else 1,
        )
        quotes = quote_collection.quotes
        quote_coverage = asdict(quote_collection.coverage)
        _record_phase(phase_timings_ms, "quotes", phase_started)

        phase_started = time.perf_counter()
        context_quotes = await provider.get_equity_quotes(scan_config.context_symbols)
        _record_phase(phase_timings_ms, "context_quotes", phase_started)
        sector_map = load_sector_map(scan_config.universe_dir)
        liquid_meta = load_liquid_meta(scan_config.universe_dir)
        reference_prices = {
            symbol: float(payload["price"])
            for symbol, payload in liquid_meta.items()
            if isinstance(payload, dict) and payload.get("price") is not None
        }

        in_premarket = is_premarket_window(generated_at, start=scan_config.premarket_start_et)

        avg_volumes: dict[str, float] = {}
        phase_started = time.perf_counter()
        if scan_config.filters.min_rvol > 0:
            rvol_symbols = symbols_needing_rvol_fetch(quotes, in_premarket=in_premarket)
            log.info(
                "equity_scan_rvol_targets universe=%d needing_rvol=%d",
                len(symbols),
                len(rvol_symbols),
            )
            avg_volumes = await fetch_avg_volumes(
                provider,
                rvol_symbols,
                lookback_days=scan_config.rvol_lookback_days,
                concurrency=scan_config.rvol_fetch_concurrency,
            )
            log.info("equity_scan_rvol_loaded symbols_with_avg_volume=%d", len(avg_volumes))
        else:
            log.info("equity_scan_rvol_skipped reason=min_rvol_disabled")
        _record_phase(phase_timings_ms, "rvol_history", phase_started)

        phase_started = time.perf_counter()
        rejected_symbols: dict[str, int] = {}
        bad_data: list[dict] = []
        snapshots = build_snapshots(
            quotes,
            symbol_map,
            scan_config,
            avg_volumes=avg_volumes,
            sector_map=sector_map,
            reference_prices=reference_prices,
            in_premarket=in_premarket,
            generated_at=generated_at,
            rejected_symbols=rejected_symbols,
            bad_data=bad_data,
        )
        preliminary_results = rank_scan_results(
            snapshots,
            settings=scan_config,
            movers_up=[],
            movers_down=[],
            market_context=[],
            scanned_symbols=len(quotes),
            generated_at=generated_at,
            quote_coverage=quote_coverage,
        )
        prior_day_symbols = _prior_day_change_symbols(preliminary_results)
        _record_phase(phase_timings_ms, "preliminary_ranking", phase_started)

        phase_started = time.perf_counter()
        prior_day_changes = await fetch_prior_day_changes(
            provider,
            prior_day_symbols,
            concurrency=scan_config.rvol_fetch_concurrency,
            days_back=scan_config.rvol_lookback_days,
        )
        _record_phase(phase_timings_ms, "prior_day_history", phase_started)

        phase_started = time.perf_counter()
        if prior_day_changes:
            # Fresh dicts, not the pass-1 ones: this re-classifies every symbol from
            # scratch against the now-accurate prior_day_pct, so reusing the pass-1
            # dicts would double-count symbols rejected the same way in both passes.
            # These become the final rejected_symbols/bad_data for reporting below.
            rejected_symbols = {}
            bad_data = []
            snapshots = build_snapshots(
                quotes,
                symbol_map,
                scan_config,
                avg_volumes=avg_volumes,
                sector_map=sector_map,
                reference_prices=reference_prices,
                prior_day_changes=prior_day_changes,
                in_premarket=in_premarket,
                generated_at=generated_at,
                rejected_symbols=rejected_symbols,
                bad_data=bad_data,
            )
        _record_phase(phase_timings_ms, "snapshot_finalize", phase_started)
        log.info("equity_scan_prior_day_changes_loaded symbols=%d", len(prior_day_changes))

        news_symbols = _news_candidate_symbols(snapshots, limit=scan_config.news.max_symbols)
        phase_started = time.perf_counter()
        news_impacts = await fetch_news_impacts(
            news_symbols,
            settings=scan_config.news,
            generated_at=generated_at,
        )
        _record_phase(phase_timings_ms, "news", phase_started)
        snapshots = attach_news_impacts(snapshots, news_impacts)
        log.info(
            "equity_scan_news_loaded candidates=%d matched=%d providers=%s",
            len(news_symbols),
            len(news_impacts),
            scan_config.news.providers,
        )

        market_context = [
            ctx
            for symbol, quote in context_quotes.items()
            if (ctx := parse_market_context(symbol, quote)) is not None
        ]

        movers_up: list[dict] = []
        movers_down: list[dict] = []
        phase_started = time.perf_counter()
        if scan_config.include_movers:

            async def _fetch_index_movers(index: str) -> tuple[list[dict], list[dict]]:
                up, down = await asyncio.gather(
                    provider.get_market_movers(index, sort_order="PERCENT_CHANGE_UP"),
                    provider.get_market_movers(index, sort_order="PERCENT_CHANGE_DOWN"),
                )
                return up, down

            # Concurrent across indexes (and up/down within each), not sequential —
            # each index is still isolated via return_exceptions, matching the
            # original's per-index try/except.
            index_results = await asyncio.gather(
                *(_fetch_index_movers(index) for index in scan_config.mover_indexes),
                return_exceptions=True,
            )
            for index, result in zip(scan_config.mover_indexes, index_results, strict=True):
                if isinstance(result, BaseException):
                    log.warning("equity_scan_movers_failed index=%s error=%s", index, result)
                    continue
                up, down = result
                movers_up.extend(up)
                movers_down.extend(down)
        _record_phase(phase_timings_ms, "movers", phase_started)

        phase_started = time.perf_counter()
        results = rank_scan_results(
            snapshots,
            settings=scan_config,
            movers_up=movers_up,
            movers_down=movers_down,
            market_context=market_context,
            scanned_symbols=len(quotes),
            generated_at=generated_at,
            rejected_symbols=rejected_symbols,
            bad_data=bad_data,
            quote_coverage=quote_coverage,
        )
        _record_phase(phase_timings_ms, "final_ranking", phase_started)

        phase_started = time.perf_counter()
        messages = build_report(results, settings=scan_config, generated_at=generated_at)
        _record_phase(phase_timings_ms, "report", phase_started)

        phase_started = time.perf_counter()
        archive_path = archive_report(
            messages,
            report_dir=scan_config.report_dir,
            generated_at=generated_at,
        )
        _record_phase(phase_timings_ms, "markdown_archive", phase_started)
        phase_timings_ms["total_generation"] = round(
            (time.perf_counter() - run_started) * 1000.0,
            3,
        )
        results = replace(results, phase_timings_ms=dict(phase_timings_ms))

        phase_started = time.perf_counter()
        json_archive_path = archive_report_json(
            results,
            report_dir=scan_config.report_dir,
            generated_at=generated_at,
        )
        _record_phase(phase_timings_ms, "json_archive", phase_started)
        log.info(
            "equity_scan_complete open_scan=%s prior_gainers=%d prior_losers=%d "
            "premarket_gainers=%d premarket_losers=%d opening_focus=%d matched_symbols=%d "
            "show_premarket=%s show_movers=%s rejected_symbols=%s messages=%d "
            "archive=%s json_archive=%s",
            open_scan,
            len(results.prior_gainers),
            len(results.prior_losers),
            len(results.premarket_gainers),
            len(results.premarket_losers),
            len(results.opening_focus),
            results.matched_symbols,
            results.show_premarket,
            results.show_movers,
            rejected_symbols,
            len(messages),
            archive_path,
            json_archive_path,
        )
        log.info("equity_scan_timings phase_timings_ms=%s", phase_timings_ms)

        if dry_run:
            for message in messages:
                print(message)
                print("\n" + ("-" * 60) + "\n")
            return messages

        if not app_settings.discord_webhook_url:
            raise RuntimeError(
                "EQUITY_SCANNER_DISCORD_WEBHOOK_URL not configured in .env or environment"
            )
        notifier = DiscordNotifier(app_settings.discord_webhook_url)
        await notifier.notify_messages(messages)
        return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the equity-scanner morning scan.")
    parser.add_argument(
        "--scan-config",
        default="configs/equity_scan.yaml",
        help="Equity scan config (universes/filters/limits/news/report settings)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report instead of posting to Discord",
    )
    parser.add_argument(
        "--open-scan",
        action="store_true",
        help="Include after-open Schwab mover buckets and Opening Focus context",
    )
    parser.add_argument(
        "--quote-coverage-mode",
        choices=("strict", "parity-bounded-recovery", "parity-paced-recovery"),
        default="strict",
        help="Quote-batch failure policy; recovery modes are for auditable parity proofs",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    asyncio.run(
        run_scan(
            scan_config_path=args.scan_config,
            dry_run=args.dry_run,
            open_scan=args.open_scan,
            quote_coverage_mode=args.quote_coverage_mode,
        )
    )


if __name__ == "__main__":
    main()
