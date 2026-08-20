# equity-scanner

Phase 1 of migrating ButterflyGuy's embedded equity scanner
(`Butterflyguy/src/butterfly_guy/equity_scan/`) off a direct `SchwabClientWrapper`
onto [SchwabGateway](https://github.com/hollowc2/SchwabGateway)'s read-only HTTP API.

Scope is deliberately small: a gateway-backed data provider, ported volume/rvol
helpers, and the provider-agnostic scan-ranking logic, proven against fakes.
No Discord notifier, no news, no universe fetching, no orchestration CLI, no
Helios deployment — those are an unscoped Phase 2.

## Layout

- `gateway.py` — factory for `schwab_gateway_sdk.GatewayMarketDataClient`.
- `provider.py` — `GatewayEquityDataProvider`: adapts the gateway's
  `get_history`/`get_movers` to the shapes ButterflyGuy's `volume.py`/`scanner.py`
  expect (candle dicts, mover dicts).
- `volume.py` — ported `fetch_avg_volumes`/`fetch_prior_day_changes` and their
  pure helpers (`avg_daily_volume`, `prior_session_pct_change`, `compute_rvol`).
- `scanner.py` — ported subset of ButterflyGuy's `equity_scan/scanner.py`
  (`EquitySnapshot`, `parse_equity_quote`, `build_snapshots`, `rank_scan_results`,
  ...), news/catalyst fields dropped since news is Phase 2.
- `scan_config.py` — trimmed settings (filters/limits) `scanner.py` needs.
- `time_utils.py` — trimmed market-hours helpers `scanner.py` needs.

## Testing

Tests run against fakes (`httpx.MockTransport` for the gateway HTTP layer, a
fake provider for the scan-logic tests) — no live Schwab credentials, no
running gateway required.

```
uv run pytest
```
