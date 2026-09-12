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
