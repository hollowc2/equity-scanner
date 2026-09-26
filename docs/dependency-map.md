# Extraction dependency map

## Scanner-owned logic

- `scanner.py`: quote normalization boundary, filters, calculated snapshots, rankings,
  filter reasons, market context, and catalyst attachment.
- `volume.py`: 20-session average volume, RVOL, and prior-session percentage change.
- `universes.py` / `refresh_universes.py`: public seed acquisition, gateway liquidity
  validation, local seed/output files, and sector metadata.
- `news.py`, `report.py`, `notifier.py`, and `run.py`: optional enrichment, Discord-size
  reporting, JSON/Markdown persistence, and orchestration.
- `scan_config.py`, `config.py`, and `time_utils.py`: scanner-owned configuration and
  market-session handling.

## Reusable neutral utilities

- Pure calculation and ranking helpers, public HTTP universe/news readers, market
  calendar helpers, file-based report persistence, and the narrow notifier interface.

## ButterflyGuy dependencies removed

- `AppConfig`, strategy runtime, logging/Prometheus globals, database pool, options
  execution, account/risk/position services, cron wrappers, and trading notifications.
- The standalone package has no runtime import or filesystem dependency on ButterflyGuy.

## Direct Schwab dependencies replaced

- `SchwabClientWrapper.get_equity_quotes` -> gateway `GET /v1/quotes`, in batches of 100.
- `SchwabClientWrapper.get_price_history` -> gateway `GET /v1/history` with
  `frequency=daily&days_back=20`.
- `SchwabClientWrapper.get_market_movers` -> gateway `GET /v1/movers`, separately for
  `up` and `down`.
- OAuth/token loading and `schwab-py` are intentionally absent; the scoped API key is
  supplied only through the environment and identifies `equity-scanner` at background
  priority in SchwabGateway. Identity and priority come from the gateway's server-side
  key record, not caller-controlled headers; deployment evidence must verify that record
  without printing the key.

## Intentional behavior differences

- Gateway quotes are already flattened to the freshest regular/extended session; the
  raw two-session Schwab payload is not reconstructed. Price selection therefore uses
  the selected session's positive `last`, then `mark`, then bid/ask midpoint.
- The regular-price versus Schwab `netPercentChange` disagreement check runs only when
  `QuoteV1.session == "regular"`. Once the gateway selects an extended session, the
  discarded regular price cannot be reconstructed and that check is unavailable.
- `QuoteV1.volume` belongs to the selected session. During premarket it becomes both
  scanner `volume` and `premarket_volume` when the selected session is extended; it is
  not the original payload's simultaneous prior regular-session volume plus extended
  volume pair. Outside premarket, extended-session volume is never labeled as
  premarket RVOL.
- `QuoteV1.close` and `net_percent_change` remain the regular-session reference fields.
  If net percent change is absent, the scanner derives it from the selected current
  price and regular close, because no separate regular last is available.
- A quote marked stale is retained because Gateway quote freshness reflects the selected
  trade event; during premarket an old last trade can coexist with otherwise usable quote
  fields. The scanner exposes this as the `gateway_stale` data-quality flag and still
  applies its normal field, liquidity, and reference-price validation. Stale history and
  mover responses remain fail-closed. Missing symbols in a partial quote batch are logged
  and omitted, making the partial result explicit without aborting all other symbols.
- Authentication/authorization and malformed-contract failures are fail-closed. Only
  transient capacity, timeout, and upstream-unavailable errors receive bounded retry.
- Gateway quote requests are deduplicated, class-share dots are translated at the
  gateway boundary, and batches are capped at the gateway contract's 100-symbol limit.

## Additive evidence differences

The standalone JSON archive adds `phase_timings_ms` and `quote_coverage`. These fields
do not alter scan selection or report text. The ButterflyGuy parity tests and
comparator were removed after cutover.

## Raw equity recording ownership

- SchwabGateway already owns the generic venue-specific Level II capture: bounded
  symbols/duration, raw frames, normalized snapshots, reconnect/continuity evidence,
  hashes, a non-overwriting manifest, and catalog/retention metadata. New Level II
  capture should use that recorder rather than ButterflyGuy's raw stream recorder.
- SchwabGateway's `schwab-gateway-capture-equity-streams` now owns bounded
  `CHART_EQUITY` and Level I raw/relabeled capture with the same short token-lock
  bootstrap, reconnect evidence, hashes, and non-overwriting manifest policy. It must
  pass local and supervised runtime proof before the ButterflyGuy implementation is
  removed.
- ButterflyGuy's one-minute candle backfill has no downstream backtest or report
  consumer in the repository. SchwabGateway's SDK-backed
  `schwab-gateway-export-session-history` now owns the generic replacement: it combines
  regular/extended one-minute bars, rejects stale/empty evidence, hashes its output,
  and never overwrites a capture. Removal still waits for supervised runtime proof.
