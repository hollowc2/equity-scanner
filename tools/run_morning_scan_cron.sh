#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
lock_file=${EQUITY_SCANNER_LOCK_FILE:-/tmp/equity-scanner-morning.lock}
image_file=${EQUITY_SCANNER_IMAGE_FILE:-$scanner_root/.candidate-image}

# The two UTC cron slots cover Pacific daylight and standard time; execute only at
# 06:00 local Pacific and never overlap another scan.
[ "$(TZ=America/Los_Angeles date +%H:%M)" = "06:00" ] || exit 0
[ -s "$image_file" ] || { echo "equity_scanner_missing_candidate_image"; exit 1; }

exec flock -n "$lock_file" sh -c '
  cd "$1"
  EQUITY_SCANNER_IMAGE="$(sed -n "1p" "$2")" \
  EQUITY_SCANNER_SECRET_ENV="$1/secrets/gateway.env" \
  docker compose -f compose.candidate.yml run --rm scan
' sh "$scanner_root" "$image_file"
