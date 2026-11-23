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
uv run equity-scanner-run --trade-setups --dry-run
```

`--dry-run` turns off every external action. Use it while testing.

Settings for universes, filters, report limits, and news are in
`configs/equity_scan.yaml`. Use `--scan-config` to point to a different file. Add
your own tickers to `configs/universes/custom.txt`, one per line.

### Trade setups

`--trade-setups` adds three sections to a one-time scan. It saves timestamped
Markdown and JSON alongside the regular morning archive.

- **SPX and volatility.** Shows SPX and VIX levels, percent changes, and quote
  timestamps. A pre-open run can show a labeled prior-session print. Once the
  first minute after the 15-minute cash opening range closes, the report uses
  SPY as a proxy for direction. It shows the 10-minute move, estimated session
  VWAP, and whether the latest close broke or held above both the range and
  VWAP, or below both. Cash-session S&P 500 breadth shows advancing, declining,
  and flat constituents alongside fresh quote coverage.
- **Daily dip watch.** Flags a completed daily loss of at least 3% with true
  range at least 1.5× the preceding 14-session ATR. The report shows the selloff
  high, low, and relative volume. A close in the top 35% of the selloff range
  gets a separate label. The next completed session can show a higher close
  with the low intact, or follow-through above the selloff high.
- **2-minute capitulation.** During cash hours, combines consecutive,
  completed 1-minute candles into 2-minute bars anchored to 9:30 ET. A flush
  makes a new session low at least 2% below the prior close, with at least
  2× median volume and 1.75× median range over the previous ten bars. A close
  above the flush high within the next two bars confirms a reclaim if the low
  holds. The scan reports active signals through that two-bar window.

The initial thresholds are screening heuristics. The daily screen checks up to
24 eligible names, prioritizing current or prior-session weakness. The intraday
screen checks up to 16 names with fresh regular-session quotes, prioritizing
names down at least 1% alongside eligible custom watchlist names. Missing
history is shown in coverage. Adjust the history and display limits with
`trade_setups.daily_candidates`, `trade_setups.intraday_candidates`, and
`trade_setups.report_limit` in the scan YAML.

### SPX butterfly snapshot

```bash
uv run equity-scanner-run --spx-snapshot --dry-run
```

The standalone snapshot reads SPX and VIX data through SchwabGateway during
the cash session. It reports the 9:30 ET SPX gap against the prior official
close, whether SPX has filled it, and the gap-rule direction. The configured
VIX buckets mirror ButterflyGuy's checked-in SPX profile and show the
daily VIX-based move estimate, active wing widths, and target centers.

Same-day SPXW quotes show the nearest ATM straddle and a quoted example
near each target center, with leg-derived midpoint, ask, and ask premium.
The examples use fresh quotes and represent pricing context. The snapshot
also shows prior-session VIX1D/VIX and VIX/VIX3M. When VIX1D daily history
is empty, it labels the gateway prior close or prior-session closing print
used instead. Missing inputs are shown as unavailable.

The production schedule adds a report at 10:00 a.m. ET on trading days.
The CLI archives timestamped reports under `spx_snapshots/` in the
configured report directory.

## Development

```bash
uv run pytest
uv run ruff check .
```

Tests use fakes only. They need no credentials or network access.

Scan and universe-refresh quote batches request the gateway's available-quote
contract. Missing symbols are recorded in coverage and logged individually without
retrying or splitting the batch. The existing limits on missing symbols and affected
batches still apply. Strict quote reads (including market context) keep their normal
contract. Authentication, malformed responses and gateway outages fail the job;
explicit upstream rate limits are propagated without automatic retries.

## Deployment

Deployment needs separate approval. See the
[deployment runbook](docs/deployment-runbook.md) and the
[dependency map](docs/dependency-map.md).
