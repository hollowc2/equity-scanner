#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
reference_root=${BUTTERFLYGUY_ROOT:-/opt/butterflyguy}
run_date=$(TZ=America/New_York date +%F)
[ "$run_date" = "${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}" ] || exit 0

parity_root="$scanner_root/parity/$run_date"
mkdir -p "$parity_root/input" "$parity_root/output"

# Freeze the exact non-secret universe/config inputs used by the authoritative job.
cp "$reference_root/configs/equity_scan.yaml" "$parity_root/input/equity_scan.reference.yaml"
cp "$reference_root/configs/universes/"*.txt "$parity_root/input/"
cp "$reference_root/configs/universes/"*.json "$parity_root/input/"

# Preserve behavior settings but point the standalone process at its frozen copy,
# enforce bounded gateway load, and disable news/notifications. The matching
# ButterflyGuy reference run uses the same parity config transformation.
python3 "$scanner_root/tools/write_parity_config.py" \
  "$parity_root/input/equity_scan.reference.yaml" \
  "$parity_root/input/equity_scan.candidate.yaml" \
  "$parity_root/input"

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
EQUITY_SCANNER_IMAGE="$image" \
EQUITY_SCANNER_SECRET_ENV="$scanner_root/secrets/gateway.env" \
EQUITY_SCANNER_UID=$(id -u) \
EQUITY_SCANNER_GID=$(id -g) \
timeout --signal=TERM --kill-after=15s "${remaining_seconds}s" \
docker compose -f "$scanner_root/compose.candidate.yml" run --rm \
  -e SCHWAB_GATEWAY_URL="$gateway_url" \
  -e SCHWAB_GATEWAY_MAX_ATTEMPTS=1 \
  -v "$parity_root/input:/app/parity-input:ro" \
  -v "$parity_root/output:/app/parity-output:rw" \
  scan --dry-run --scan-config /app/parity-input/equity_scan.candidate.yaml \
  > "$parity_root/output/standalone.log" 2>&1

cp "$scanner_root/reports/equity_scans/$run_date.json" \
  "$parity_root/output/standalone.json"
