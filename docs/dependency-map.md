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
  priority in SchwabGateway.

## Intentional behavior differences

- Gateway quotes are already flattened to the freshest regular/extended session; the
  raw two-session Schwab payload is not reconstructed.
- A gateway response marked stale is rejected. Missing symbols in a partial quote batch
  are logged and omitted, making the partial result explicit without aborting all other
  symbols.
- Authentication/authorization and malformed-contract failures are fail-closed. Only
  transient capacity, timeout, and upstream-unavailable errors receive bounded retry.
