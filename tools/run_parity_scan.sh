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
# enforce the gateway's 100-symbol transport boundary, and disable news/notifications.
python3 "$scanner_root/tools/write_parity_config.py" \
  "$parity_root/input/equity_scan.reference.yaml" \
  "$parity_root/input/equity_scan.candidate.yaml" \
  "$parity_root/input"

image=$(sed -n '1p' "$scanner_root/.candidate-image")
EQUITY_SCANNER_IMAGE="$image" \
EQUITY_SCANNER_SECRET_ENV="$scanner_root/secrets/gateway.env" \
EQUITY_SCANNER_UID=$(id -u) \
EQUITY_SCANNER_GID=$(id -g) \
docker compose -f "$scanner_root/compose.candidate.yml" run --rm \
  -v "$parity_root/input:/app/parity-input:ro" \
  -v "$parity_root/output:/app/parity-output:rw" \
  scan --dry-run --scan-config /app/parity-input/equity_scan.candidate.yaml \
  > "$parity_root/output/standalone.log" 2>&1

cp "$scanner_root/reports/equity_scans/$run_date.json" \
  "$parity_root/output/standalone.json"
