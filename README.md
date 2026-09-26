<p align="center">
  <img src="logo.jpg" alt="EquityScanner — neon shield and candlestick mark" width="300">
</p>

# EquityScanner

EquityScanner finds the day's premarket and opening movers and posts a morning
report to Discord.

It scans the S&P 500, the Nasdaq-100, a liquidity-filtered universe, and your own
watchlist. It ranks the biggest movers, adds recent news and filings, and saves
each report as dated Markdown and JSON.

EquityScanner only reads market data. It has no trading, account, or order code,
and it gets all Schwab data through the read-only
[SchwabGateway](https://github.com/hollowc2/SchwabGateway) API.

## Setup

1. Copy `.env.example` to `.env` and set your gateway URL and API key.
2. Optional: add `SEC_USER_AGENT` and `ALPHA_VANTAGE_API_KEY` for news, and
   `EQUITY_SCANNER_DISCORD_WEBHOOK_URL` to post reports.
3. Build the symbol lists:

   ```bash
   uv run equity-scanner-refresh-universes
   ```

## Usage

```bash
uv run equity-scanner-run --dry-run      # print the report; post nothing
uv run equity-scanner-run                # post the report to Discord
uv run equity-scanner-run --open-scan    # add after-open movers
```

`--dry-run` turns off every external action. Use it while testing.

Settings for universes, filters, report limits, and news are in
`configs/equity_scan.yaml`. Use `--scan-config` to point to a different file. Add
your own tickers to `configs/universes/custom.txt`, one per line.

## Development

```bash
uv run pytest
uv run ruff check .
```

Tests use fakes only. They need no credentials or network access.

## Deployment

Deployment needs separate approval. See the
[deployment runbook](docs/deployment-runbook.md) and the
[dependency map](docs/dependency-map.md).
