#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
reference_root=${BUTTERFLYGUY_ROOT:-/opt/butterflyguy}
run_date=${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}
[ "$(date -d "$run_date" +%F)" = "$run_date" ] || {
  echo "parity_input_invalid_date"
  exit 1
}

parity_root="$scanner_root/parity/$run_date"
input_root="$parity_root/input"
[ ! -e "$input_root" ] || { echo "parity_input_already_frozen"; exit 1; }

mkdir -p "$parity_root"
stage="$parity_root/.input-staging.$$"
mkdir "$stage"
trap 'rm -rf "$stage"' EXIT HUP INT TERM

for name in sp500.txt nq100.txt liquid.txt custom.txt sectors.json liquid_meta.json; do
  source_file="$reference_root/configs/universes/$name"
  [ -f "$source_file" ] || { echo "parity_input_missing_$name"; exit 1; }
  cp "$source_file" "$stage/$name"
done

python3 "$scanner_root/tools/write_parity_config.py" \
  "$reference_root/configs/equity_scan.yaml" \
  "$stage/equity_scan.reference.yaml" \
  "$input_root" \
  --custom-watchlist "$input_root/custom.txt" \
  --report-dir /opt/butterflyguy/reports/equity_scans

python3 "$scanner_root/tools/write_parity_config.py" \
  "$stage/equity_scan.reference.yaml" \
  "$stage/equity_scan.candidate.yaml" \
  /app/parity-input \
  --custom-watchlist /app/parity-input/custom.txt \
  --report-dir reports/equity_scans

(
  cd "$stage"
  sha256sum \
    equity_scan.reference.yaml equity_scan.candidate.yaml \
    sp500.txt nq100.txt liquid.txt custom.txt sectors.json liquid_meta.json \
    > input.sha256
)

chmod 0444 "$stage"/*
chmod 0555 "$stage"
mv "$stage" "$input_root"
trap - EXIT HUP INT TERM
echo "parity_input_frozen path=$input_root"
