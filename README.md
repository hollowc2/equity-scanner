<p align="center">
  <img src="logo.jpg" alt="EquityScanner — neon shield and candlestick mark" width="300">
</p>

# equity-scanner

Read-only market-data scanner: premarket and opening movers across the S&P 500,
Nasdaq-100, a liquidity-filtered universe, and a custom watchlist, with news
enrichment and Discord reporting. Standalone — no runtime dependency on
ButterflyGuy — backed by [SchwabGateway](https://github.com/hollowc2/SchwabGateway)'s
read-only HTTP API.

One CLI run does the whole morning scan: fetch the universe, pull gateway quotes
and daily history, rank snapshots, enrich with news, and post a Discord report
with dated markdown/JSON archives. The scanner never writes market data or talks
to Schwab directly, and `--dry-run` disables every external action.

## Quick start

```
uv run equity-scanner-refresh-universes   # first run: populate data/universes/
uv run equity-scanner-run --dry-run       # print the report instead of posting
uv run equity-scanner-run --open-scan     # include after-open Schwab mover buckets
```

Both accept `--scan-config path/to/equity_scan.yaml`. See `.env.example` for
required/optional secrets.

## Testing

```
uv run pytest
uv run ruff check .
```

Tests run entirely against fakes (mocked gateway HTTP, recorded universe fixtures,
patched Discord notifier) — no live credentials or network calls needed.

## Deployment

Deployment and schedule migration require separate approval and are gated
behind their own runbook — see [`docs/deployment-runbook.md`](docs/deployment-runbook.md)
for the candidate/parity/schedule/rollback process and
[`docs/dependency-map.md`](docs/dependency-map.md) for the ButterflyGuy extraction
boundary and behavior differences.
