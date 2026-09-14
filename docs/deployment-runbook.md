# EquityScanner candidate and schedule migration

This runbook has two explicit approval gates. Nothing in the repository installs cron,
issues or copies credentials, deploys a container, restarts a service, or posts a
notification automatically.

## Local candidate preparation (no live approval required)

1. Record clean/dirty status and exact commits for EquityScanner, ButterflyGuy, and the
   pinned SchwabGateway SDK.
2. Run `uv run pytest`, `uv run ruff check .`, and `uv build` in EquityScanner. Run the
   full ButterflyGuy suite and lint pass. If SchwabGateway changes, run its suite, lint,
   gateway build, and SDK build too.
3. Build the candidate locally with a unique tag. Resolve its immutable local image ID
   with `docker image inspect --format '{{.Id}}' TAG`; write only that ID (never a
   credential) to the deployment host's `.candidate-image` during the separately
   approved deployment.
4. Validate `compose.candidate.yml` with the image ID and a placeholder env-file path.
   The `scan` and `refresh-universes` services must remain one-shot, read-only,
   capability-dropped, host-networked, and dry-run-only. Candidate universe data stays
   read-only.
5. Preserve the source commit, SDK commit, image ID, test output, and Compose rendering
   with the candidate evidence. Do not overwrite historical scan or universe files.

## Approval gate 1: deploy and run same-session parity

Required approval must explicitly cover candidate deployment, the scanner-owned
background gateway credential, and the bounded parity schedule. It does not authorize
production notification or replacing either existing schedule.

After approval:

1. Copy the reviewed files and immutable image to `/opt/equity-scanner` without changing
   the running gateway. Create or install a scanner-owned key with only
   `market_data:read`, application identity `equity-scanner`, and background priority;
   never print or place the key in evidence.
2. Freeze the reference config and all six universe files with
   `tools/prepare_parity_input.sh`. Its manifest must verify before either run.
3. Let ButterflyGuy produce the dry-run reference first, then execute
   `tools/run_parity_scan.sh`. The candidate must stay dry-run and finish before its
   deadline. Run `tools/compare_parity_scan.sh` offline afterward.
4. Require 100% quote coverage, internally consistent coverage counts, acceptable
   documented `QuoteV1` differences, and a passing stable gate. Preserve partial output
   on any failure; do not call incomplete coverage parity.
5. Inspect gateway metrics/logs over the candidate window. Protected request rejects,
   protected queue timeouts, protected latency regression, order-book subscriber drops,
   and gateway readiness degradation must remain absent. Attribute background 429/503
   responses to the candidate evidence and require bounded recovery.
6. Confirm the candidate made no Discord call and that only the existing ButterflyGuy
   schedule owns notification delivery.

Rollback at this gate: remove only the uninstalled/one-day candidate cron entry if it
was staged, stop no shared service, leave ButterflyGuy cron unchanged, revoke only the
scanner-owned key through the gateway's approved key-management procedure, and preserve
the image plus parity evidence.

## Approval gate 2: replace the production schedules

This requires a second explicit approval after gate 1 passes. Before mutation, capture
the exact existing crontab and identify both current ButterflyGuy entries: morning scan
and weekly universe refresh.

The reviewed production change must make exactly one owner for each schedule:

- EquityScanner morning scan at 06:00 America/Los_Angeles on weekdays, with notification
  enabled exactly once.
- EquityScanner universe refresh at 20:00 America/Los_Angeles on Sunday, with its data
  mount writable only for this job and no `--dry-run` argument.

Install the new entries and comment out or otherwise disable the old ButterflyGuy lines;
do not delete the captured crontab or historical logs. Verify the effective crontab has
one executable owner per job. Observe the first scheduled scan and refresh: require
successful output/archive delivery, unchanged historical files except the intended new
dated output, no duplicate notification, and no protected gateway degradation.

Rollback at this gate: restore the captured ButterflyGuy crontab verbatim, disable the
EquityScanner entries, return the candidate to dry-run, and retain all logs/reports for
diagnosis. Do not delete either code path until a successful scheduled observation is
preserved.

## Removal gate

Only after both schedules pass may ButterflyGuy's duplicated scanner modules, scripts,
configs, cron definitions, focused tests, and current architecture references be
removed. Preserve `reports/equity_scans`, universe evidence, databases, captures, and
market data. Run ButterflyGuy's full suite, lint, Compose validation, reference searches,
and graph update after removal.

Raw recording is a separate gate. SchwabGateway's existing order-book recorder replaces
the venue Level II portion today. Its new Level I/chart recorder and generic
session-history exporter are the intended owners of the remaining behavior, but they
still require supervised runtime proof. Do not remove ButterflyGuy's raw recorder/helper
or candle backfill until that proof is preserved as described in `dependency-map.md`.


## Production deployment prepared 2026-09-13

The explicit migration request authorizes replacing both ButterflyGuy schedules on
Helios. Production definitions are `compose.production.yml`,
`configs/equity_scan.production.yaml`, and `infra/equity_scanner_production.cron`.
The separate production wrappers use `.production-image`; candidate definitions remain
available for rollback and investigation.

Verified baseline:

- Scanner source: `0e9d60761892ad493571822ba3b62ebae7fa44b3`.
- Installed image: `sha256:08cb6979962cb6f4a3b50dd287bbc704c2356ab849e2c24c814e392799cad331`.
- SDK pin: `5b8f1d76aa9fb97a300172d39e7afcc4ab60e83a`.
- ButterflyGuy release: `929429978a09c153afb5260771eff84386762ce4`.
- September 10 sequential-skew-aware comparison reports pass. The current release
  also has a September 14 one-day parity schedule installed.
- Gateway container `schwab_gateway_live` is healthy, port 8011.
- Existing scanner secret file provides gateway settings only. The production host
  launcher reads only `EQUITY_DISCORD_WEBHOOK_URL`, `SEC_USER_AGENT`, and
  `ALPHA_VANTAGE_API_KEY` from `/opt/butterflyguy/.env`, mapping the equity webhook
  to the scanner environment name in memory. No credential file is changed or
  duplicated, and unrelated settings are not passed to the container. Override
  `EQUITY_SCANNER_NOTIFICATION_ENV` to select another external file with these names.
  This is an external configuration dependency; no ButterflyGuy runtime is used.

Cutover procedure once notification configuration is resolved:

1. Create a timestamped deployment backup directory on Helios; capture `crontab -l`
   verbatim into `crontab.before` with mode 0600. Record image and source identifiers.
2. Install the production Compose, config, and wrappers. Set `.production-image` to
   the verified immutable image above. Initialize `data/universes` from the current
   ButterflyGuy universe files (market data only), preserving custom watchlist and
   sector metadata. Record input checksums. Preserve all historical files.
3. Validate Compose without displaying resolved environments. Run a bounded dry-run
   scan into a separate validation report directory with production config and strict
   quote coverage; verify full coverage and gateway health/metrics before and after.
   Validate refresh with `--dry-run` before enabling its writable production job.
4. Comment out exactly the old ButterflyGuy morning-scan and universe-refresh command
   lines. Disable the September 14 scanner parity commands because they depend on the
   replaced reference owner. Install the two production cron lines. Compare against
   the captured crontab and assert one executable owner per production job.
5. Observe the first scheduled archive and Discord delivery, and the first scheduled
   refresh. A completed deployment is distinct from those future observations.

Rollback: run `crontab /opt/equity-scanner/.deploy-backups/<cutover>/crontab.before`.
This restores the original schedule owners and removes the production entries. If a
new one-shot scanner job is still running, stop only that identified production job
before restoring notification ownership. Leave gateway, trading containers, database,
secrets, historical reports, and universe evidence intact. No gateway rebuild,
restart, credential change, trading change, or database migration is part of cutover.


Production validation findings (2026-09-13, before cron installation):

- Existing daily-scans webhook validated through a read-only metadata request.
- Normal Sunday scan correctly skips non-trading days.
- Initial full dry-run scan and refresh failed with gateway errors. Running the
  checks together also caused background queue pressure. Production now uses one
  quote request at a time, one history request at a time, a 15-second SDK timeout,
  and a shared production-job lock. Strict complete-coverage behavior is unchanged.
- `quote_fetch_concurrency` defaults to four for existing configs and is constrained
  to 1–4; production explicitly selects one.
- Docker build context uses an allowlist so external secrets and deployment backups
  cannot enter an image build.
- Local validation: 117 tests passed, Ruff passed, package build passed.
- Revised image and sequential live validation remain required before installing
  `crontab.proposed`; staging files is not a successful production cutover.


The sequential full-scan validation passed with all 1,897 requested symbols returned,
zero failed quote batches, and one concurrent quote call. Its Sunday quotes were all
marked stale; this validates plumbing, not live premarket timeliness. The validation
archive is isolated under `reports/production-validation/report`.

The exchange-wide refresh exposed two concrete listing-parser defects: `.R` rights
(e.g. `AIIA.R`, translated to `AIIA/R`) and an ACT symbol that looks like a class share
(`NE.A`) but whose CQS symbol identifies a warrant (`NE.WS.A`). Both are outside the
common-stock universe. The parser now excludes rights and checks the NYSE CQS symbol
for warrant designators while preserving real class shares such as `BRK.A`.
Regression tests cover these cases. Current local suite: 121 passed; Ruff and package
build passed. Refresh validation must pass on the corrected image before cron cutover.
