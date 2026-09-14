"""Refresh equity universe files (sp500, nq100, liquid) — CLI entry point
(`equity-scanner-refresh-universes`), ported from ButterflyGuy's
scripts/refresh_equity_universes.py. equity-scanner owns this refresh itself rather
than reading ButterflyGuy's output — see universes.py's module docstring."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from equity_scanner.config import AppSettings
from equity_scanner.gateway import build_gateway_client
from equity_scanner.provider import GatewayEquityDataProvider
from equity_scanner.scan_config import load_equity_scan_config
from equity_scanner.universes import (
    build_liquid_meta,
    fetch_exchange_seed_map,
    filter_symbols_by_avg_volume,
    filter_symbols_by_price,
    refresh_builtin_universes,
    write_liquid_meta,
    write_universe_file,
)
from equity_scanner.volume import fetch_avg_volumes

log = logging.getLogger("equity_scanner.refresh_universes")


async def refresh_liquid_universe(
    *,
    universe_dir: str | Path,
    provider: GatewayEquityDataProvider,
    min_price: float,
    min_avg_volume: float,
    batch_size: int,
    rvol_lookback_days: int,
    rvol_fetch_concurrency: int,
    dry_run: bool = False,
    quote_fetch_concurrency: int = 4,
) -> dict[str, int]:
    """Build liquid.txt from exchange seeds validated via gateway quotes and volume."""
    base = Path(universe_dir)
    exchange_map = fetch_exchange_seed_map()
    seed_symbols = sorted(exchange_map)
    log.info("liquid_universe_seed_loaded symbols=%d", len(seed_symbols))

    quotes = await provider.get_equity_quotes(
        seed_symbols, batch_size=batch_size, concurrency=quote_fetch_concurrency
    )
    price_passed, prices = filter_symbols_by_price(
        seed_symbols,
        quotes,
        min_price=min_price,
    )
    log.info(
        "liquid_universe_price_filtered seed=%d passed=%d min_price=%s",
        len(seed_symbols),
        len(price_passed),
        min_price,
    )

    avg_volumes = await fetch_avg_volumes(
        provider,
        price_passed,
        lookback_days=rvol_lookback_days,
        concurrency=rvol_fetch_concurrency,
    )
    final_symbols = filter_symbols_by_avg_volume(
        price_passed,
        avg_volumes,
        min_avg_volume=min_avg_volume,
    )
    log.info(
        "liquid_universe_volume_filtered passed=%d final=%d min_avg_volume=%s",
        len(price_passed),
        len(final_symbols),
        min_avg_volume,
    )

    counts = {
        "seed": len(seed_symbols),
        "post_price": len(price_passed),
        "liquid": len(final_symbols),
    }
    if dry_run:
        return counts

    write_universe_file(base / "liquid.txt", final_symbols)
    meta = build_liquid_meta(
        final_symbols,
        prices=prices,
        avg_volumes=avg_volumes,
        exchange_map=exchange_map,
    )
    write_liquid_meta(base / "liquid_meta.json", meta)
    return counts


async def run_refresh(
    *,
    scan_config_path: str,
    liquid_only: bool,
    dry_run: bool,
    min_price: float | None,
    min_avg_volume: float | None,
) -> dict[str, int]:
    scan_config = load_equity_scan_config(scan_config_path)
    counts: dict[str, int] = {}

    if not liquid_only:
        counts.update(
            refresh_builtin_universes(scan_config.universe_dir, dry_run=dry_run)
        )

    app_settings = AppSettings()
    async with build_gateway_client(app_settings) as gateway:
        provider = GatewayEquityDataProvider(
            gateway,
            max_attempts=app_settings.gateway_max_attempts,
            retry_backoff_seconds=app_settings.gateway_retry_backoff_seconds,
        )
        liquid_counts = await refresh_liquid_universe(
            universe_dir=scan_config.universe_dir,
            provider=provider,
            min_price=min_price if min_price is not None else scan_config.filters.min_price,
            min_avg_volume=(
                min_avg_volume
                if min_avg_volume is not None
                else float(scan_config.filters.min_volume)
            ),
            batch_size=scan_config.batch_size,
            quote_fetch_concurrency=scan_config.quote_fetch_concurrency,
            rvol_lookback_days=scan_config.rvol_lookback_days,
            rvol_fetch_concurrency=scan_config.rvol_fetch_concurrency,
            dry_run=dry_run,
        )
        counts.update(liquid_counts)

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh sp500.txt, nq100.txt, and liquid.txt universe files.",
    )
    parser.add_argument(
        "--scan-config",
        default="configs/equity_scan.yaml",
        help="Equity scan config",
    )
    parser.add_argument(
        "--liquid-only",
        action="store_true",
        help="Skip sp500/nq100 refresh; rebuild liquid universe only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts without writing universe files",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="Override minimum price for liquid universe (default: scan config)",
    )
    parser.add_argument(
        "--min-avg-volume",
        type=float,
        default=None,
        help="Override minimum 20d avg volume for liquid universe (default: scan config)",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    counts = asyncio.run(
        run_refresh(
            scan_config_path=args.scan_config,
            liquid_only=args.liquid_only,
            dry_run=args.dry_run,
            min_price=args.min_price,
            min_avg_volume=args.min_avg_volume,
        )
    )
    action = "Dry-run" if args.dry_run else "Refreshed"
    print(f"{action} universes: {counts}")


if __name__ == "__main__":
    main()
