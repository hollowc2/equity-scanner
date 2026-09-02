#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
run_date=$(TZ=America/New_York date +%F)
[ "$run_date" = "${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}" ] || exit 0

parity_root="$scanner_root/parity/$run_date"
input_root="$parity_root/input"
mkdir -p "$parity_root/output"
for name in \
  equity_scan.reference.yaml equity_scan.candidate.yaml input.sha256 \
  sp500.txt nq100.txt liquid.txt custom.txt sectors.json liquid_meta.json
do
  [ -f "$input_root/$name" ] || { echo "parity_frozen_input_missing_$name"; exit 1; }
done
[ -s "$input_root/equity_scan.reference.yaml" ] || exit 1
[ -s "$input_root/equity_scan.candidate.yaml" ] || exit 1
[ -s "$input_root/input.sha256" ] || exit 1
(cd "$input_root" && sha256sum --check --strict input.sha256)

image=$(sed -n '1p' "$scanner_root/.candidate-image")
# Parity shares the production read-only gateway after the retired candidate
# endpoint is removed. Override only the non-secret URL; the scanner-owned key
# still comes from its locked external env file.
gateway_url=${EQUITY_SCANNER_PARITY_GATEWAY_URL:-http://127.0.0.1:8011}
deadline_epoch=$(TZ=America/New_York date -d "$run_date 09:20:00" +%s)
remaining_seconds=$((deadline_epoch - $(date +%s)))
[ "$remaining_seconds" -gt 0 ] || {
  echo "parity_scan_deadline_elapsed"
  exit 1
}
# The paced parity mode serializes all 20 initial quote batches and permits one
# delayed sequential recovery call for each of at most three transient failures.
# Incomplete coverage is archived for the comparator to reject; normal scanner
# behavior remains strict.
EQUITY_SCANNER_IMAGE="$image" \
EQUITY_SCANNER_SECRET_ENV="$scanner_root/secrets/gateway.env" \
EQUITY_SCANNER_UID=$(id -u) \
EQUITY_SCANNER_GID=$(id -g) \
timeout --signal=TERM --kill-after=15s "${remaining_seconds}s" \
docker compose -f "$scanner_root/compose.candidate.yml" run --rm \
  -e SCHWAB_GATEWAY_URL="$gateway_url" \
  -e SCHWAB_GATEWAY_MAX_ATTEMPTS=1 \
  -v "$input_root:/app/parity-input:ro" \
  -v "$parity_root/output:/app/parity-output:rw" \
  scan --dry-run --quote-coverage-mode parity-paced-recovery \
  --scan-config /app/parity-input/equity_scan.candidate.yaml \
  > "$parity_root/output/standalone.log" 2>&1

cp "$scanner_root/reports/equity_scans/$run_date.json" \
  "$parity_root/output/standalone.json"
