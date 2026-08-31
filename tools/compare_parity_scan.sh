#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
reference_root=${BUTTERFLYGUY_ROOT:-/opt/butterflyguy}
run_date=$(TZ=America/New_York date +%F)
[ "$run_date" = "${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}" ] || exit 0

parity_root="$scanner_root/parity/$run_date/output"
reference="$reference_root/reports/equity_scans/$run_date.json"
candidate="$parity_root/standalone.json"
[ -s "$reference" ] || { echo "parity_reference_missing"; exit 1; }
[ -s "$candidate" ] || { echo "parity_candidate_missing"; exit 1; }

docker run --rm --user "$(id -u):$(id -g)" \
  --entrypoint /app/.venv/bin/equity-scanner-compare-parity \
  -v "$reference:/parity/reference.json:ro" \
  -v "$candidate:/parity/candidate.json:ro" \
  -v "$parity_root:/parity/output:rw" \
  "$(sed -n '1p' "$scanner_root/.candidate-image")" \
  /parity/reference.json /parity/candidate.json \
  --output /parity/output/comparison.json

python3 -c '
import json
import sys

result = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if result.get("verdict") == "pass" else 1)
' "$parity_root/comparison.json" || {
  echo "parity_verdict_difference"
  exit 1
}
