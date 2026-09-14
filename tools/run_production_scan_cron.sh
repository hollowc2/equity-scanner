#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
lock_file=${EQUITY_SCANNER_LOCK_FILE:-/tmp/equity-scanner-production.lock}
image_file=${EQUITY_SCANNER_IMAGE_FILE:-$scanner_root/.production-image}
gateway_url=${EQUITY_SCANNER_GATEWAY_URL:-http://127.0.0.1:8011}

# The two UTC cron slots cover Pacific daylight and standard time; execute only at
# 06:00 local Pacific and never overlap another scan.
[ "$(TZ=America/Los_Angeles date +%u)" -le 5 ] || exit 0
[ "$(TZ=America/Los_Angeles date +%H:%M)" = "06:00" ] || exit 0
[ -s "$image_file" ] || { echo "equity_scanner_missing_production_image"; exit 1; }

export EQUITY_SCANNER_NOTIFICATION_ENV="${EQUITY_SCANNER_NOTIFICATION_ENV:-/opt/butterflyguy/.env}"

exec flock -n "$lock_file" sh -c '
  cd "$1"
  EQUITY_SCANNER_IMAGE="$(sed -n "1p" "$2")" \
  EQUITY_SCANNER_SECRET_ENV="$1/secrets/gateway.env" \
  EQUITY_SCANNER_GATEWAY_URL="$3" \
  python3 "$1/src/equity_scanner/production_job.py"
' sh "$scanner_root" "$image_file" "$gateway_url"
