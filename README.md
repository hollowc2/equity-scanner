# equity-scanner

Migrating ButterflyGuy's embedded equity scanner
(`Butterflyguy/src/butterfly_guy/equity_scan/`) off a direct `SchwabClientWrapper`
onto [SchwabGateway](https://github.com/hollowc2/SchwabGateway)'s read-only HTTP API.

This standalone extraction provides a runnable morning scan: universe fetching (S&P 500,
Nasdaq-100, a liquidity-filtered universe, and a custom watchlist), news enrichment
(SEC EDGAR + Alpha Vantage), Discord reporting, and a CLI that wires it all together
end to end. It is gateway-backed and has no runtime dependency on ButterflyGuy. Helios deployment
is out of scope for this repo's code and is a separate, explicitly gated checkpoint
(see Deployment below) — not something either phase does automatically.

## Layout

- `gateway.py` — factory for `schwab_gateway_sdk.GatewayMarketDataClient`.
- `provider.py` — `GatewayEquityDataProvider`: adapts the gateway's
  `get_history`/`get_movers`/`get_quotes` to the shapes ButterflyGuy's
  `volume.py`/`scanner.py`/`universes.py` expect (candle dicts, mover dicts, and a
  `symbol -> QuoteV1` map, batched at the gateway's 100-symbols-per-request cap).
  Daily-history requests are coalesced and cached for one provider/run so the RVOL
  and prior-day phases do not refetch the same symbol/window.
  Quote batches normally remain fail-closed. The explicit
  `parity-bounded-recovery` coverage mode settles every initial batch, retains
  successful results, and makes one delayed sequential recovery call only when
  exactly one batch has a transient gateway failure. It never retries multiple
  failed batches. The separately named `parity-paced-recovery` mode serializes
  initial quote batches with a 250ms inter-batch delay, then makes one delayed,
  sequential recovery call per transient failed batch when at most three batches
  failed. This caps a 1,917-symbol universe at 20 initial plus three recovery calls.
- `volume.py` — ported `fetch_avg_volumes`/`fetch_prior_day_changes` and their pure
  helpers (`avg_daily_volume`, `prior_session_pct_change`, `compute_rvol`,
  `symbols_needing_rvol_fetch`).
- `scanner.py` — ported `equity_scan/scanner.py`: `EquitySnapshot`,
  `parse_equity_quote`, `build_snapshots`, `rank_scan_results`,
  `attach_news_impacts`, `rank_catalyst_watch`, ... **Quote parsing is rewritten, not
  a mechanical port** — see the module docstring for why: SchwabGateway's
  `/v1/quotes` returns a flat, already session-resolved `QuoteV1` per symbol (the
  gateway itself now picks the fresher of Schwab's regular/extended sessions
  server-side), not ButterflyGuy's raw two-session `{"quote", "extended"}` payload.
  Two real behavior changes fall out of that, documented on `parse_equity_quote`.
- `universes.py` — ported `equity_scan/universes.py`: S&P 500 (GitHub CSV) /
  Nasdaq-100 (header-identified Wikipedia table) / NASDAQ+NYSE listed-symbol (nasdaqtrader.com)
  fetchers, local universe file I/O, and the liquid-universe price/volume filters
  (adapted for `QuoteV1`). equity-scanner refreshes its own universe files rather
  than reading ButterflyGuy's — see the module docstring for why. Refreshes reject
  implausibly small upstream results, replace files atomically, and perform no writes
  under `--dry-run`.
- `news.py` — ported `equity_scan/news.py`: SEC EDGAR full-text search +
  company-facts, and Alpha Vantage news/earnings, keyed by ticker. Zero
  Schwab/gateway dependency. Per-provider concurrency is bounded; SEC request starts
  remain rate-spaced through `sec_request_interval_seconds`.
- `report.py` — ported `equity_scan/report.py`: formats `ScanResults` into
  Discord-message-sized (2000-char) chunks, plus dated markdown/JSON archiving.
- `notifier.py` — **narrow** port of `services/notifier.py`'s `DiscordNotifier`:
  only `_post`/`notify_messages`. The rest of that class is coupled to
  butterfly-options-trade notifications and doesn't apply here.
- `run.py` — CLI orchestration (`equity-scanner-run`), ported from
  `scripts/run_morning_scan.py`: universes -> quotes -> volume -> snapshots ->
  ranking -> news -> report -> archive -> Discord. Every material phase logs elapsed
  milliseconds, and generation timings are persisted in the JSON archive under
  `phase_timings_ms`.
- `refresh_universes.py` — CLI (`equity-scanner-refresh-universes`), ported from
  `scripts/refresh_equity_universes.py`: refreshes `sp500.txt`/`nq100.txt`/
  `sectors.json`/`liquid.txt`/`liquid_meta.json`.
- `scan_config.py` — settings (filters/limits/universes/news/report), YAML-loadable
  via `load_equity_scan_config`.
- `config.py` — `AppSettings`: gateway credentials (required, fail-closed) plus
  `SEC_USER_AGENT`/`ALPHA_VANTAGE_API_KEY`/`EQUITY_SCANNER_DISCORD_WEBHOOK_URL`
  (all optional — news providers and Discord posting degrade gracefully when unset;
  see `.env.example`).
- `time_utils.py` — market-hours helpers.

## Running it

```
uv run equity-scanner-refresh-universes   # first run: populate data/universes/
uv run equity-scanner-run --dry-run       # print the report instead of posting
uv run equity-scanner-run --open-scan     # include after-open Schwab mover buckets
```

Both accept `--scan-config path/to/equity_scan.yaml`; unset fields fall back to
`scan_config.py`'s defaults. See `.env.example` for the required/optional secrets.
The checked-in config is `configs/equity_scan.yaml`; all external actions are disabled
by using `--dry-run`.

Parity runs additionally pass
`--quote-coverage-mode parity-paced-recovery`. Their JSON report records requested,
returned, stale-retained, and unavailable symbols; failed batch IDs, symbols, and
typed errors; initial/recovery call counts and limits; pacing delays; and maximum
quote concurrency. Coverage must be exactly 100% for either strict or
`sequential-skew-aware` comparison to pass. If a recovery call fails, a non-transient
batch fails, or more than three initial batches fail, the scanner still writes the
partial evidence with `scanned_symbols` equal to the number of returned universe
symbols, and the comparator fails with `incomplete_quote_coverage`.

The skew-aware gate treats membership in all four ranked sections as capture-time
evidence because every section is built only after current price, volume, reference-
price, and premarket-RVOL filters run. For the nominally prior-day sections, it still
gates the prior-day values, gain-versus-loss section assignment, and relative ordering
of symbols present in both captures. Exact prior-section membership remains gated in
strict mode. Stale-retained quote counts and symbols remain visible in coverage
evidence and do not relax the requirement for complete quote coverage.

## Testing

Tests run against fakes (`httpx.MockTransport` for the gateway HTTP layer,
recorded-fixture responses via a monkeypatched `urllib.request.urlopen` for the
universe network fetchers, patched `_post` for the Discord notifier) — no live
Schwab credentials, no running gateway, no live network calls.

The deterministic cross-repository parity tests cover universe loading, quote-driven
filtering/ranking, RVOL and prior-session calculations, news enrichment, opening and
catalyst ranking, mover sections, Discord chunking, and Markdown/JSON archives. The
strict report comparator gates every ranked and mover section plus news payloads. The
same-session comparator separately records capture-sensitive differences caused by
sequential quote collection.

```
uv run pytest
uv run ruff check .
```

## Deployment

Deployment and schedule migration require separate approval. The current candidate
gateway is never modified by this project. See `docs/dependency-map.md` for the
extraction boundary and documented behavior differences, and
`docs/deployment-runbook.md` for the candidate, parity, schedule, and rollback gates.

`compose.candidate.yml` is a dry-run-only, one-shot candidate definition. It requires
an immutable `EQUITY_SCANNER_IMAGE` and an external scanner-owned secret env file.
The scheduled wrapper targets the production read-only gateway at `127.0.0.1:8011`
by default; `EQUITY_SCANNER_GATEWAY_URL` can override that non-secret endpoint.
`infra/equity_scanner_candidate.cron` and
`infra/equity_scanner_universe_refresh_candidate.cron` remain uninstalled. Both
candidate services are dry-run-only; the refresh service also mounts universe data
read-only.
