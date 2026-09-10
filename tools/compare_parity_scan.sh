#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
reference_root=${BUTTERFLYGUY_ROOT:-/opt/butterflyguy}
run_date=$(TZ=America/New_York date +%F)
[ "$run_date" = "${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}" ] || exit 0

parity_run_root="$scanner_root/parity/$run_date"
input_root="$parity_run_root/input"
parity_root="$parity_run_root/output"
reference="$reference_root/reports/equity_scans/$run_date.json"
candidate="$parity_root/standalone.json"
[ -s "$reference" ] || { echo "parity_reference_missing"; exit 1; }
[ -s "$candidate" ] || { echo "parity_candidate_missing"; exit 1; }
[ -s "$input_root/input.sha256" ] || { echo "parity_input_manifest_missing"; exit 1; }
(cd "$input_root" && sha256sum --check --strict input.sha256)

python3 "$scanner_root/src/equity_scanner/parity.py" \
  "$reference" "$candidate" \
  --mode sequential-skew-aware \
  --reference-config "$input_root/equity_scan.reference.yaml" \
  --candidate-config "$input_root/equity_scan.candidate.yaml" \
  --reference-universe-dir "$input_root" \
  --candidate-universe-dir "$input_root" \
  --input-manifest "$input_root/input.sha256" \
  --output "$parity_root/comparison.json"

python3 -c '
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if result.get("stable_gate_verdict") == "pass" else 1)
' "$parity_root/comparison.json" || {
  echo "parity_verdict_difference"
  exit 1
}
