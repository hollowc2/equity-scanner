#!/bin/sh
set -eu

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
lock_file=${EQUITY_SCANNER_REFRESH_LOCK_FILE:-/tmp/equity-scanner-production.lock}
image_file=${EQUITY_SCANNER_IMAGE_FILE:-$scanner_root/.production-image}
gateway_url=${EQUITY_SCANNER_GATEWAY_URL:-http://127.0.0.1:8011}

# The two UTC slots cover Sunday 20:00 Pacific across daylight saving changes.
[ "$(TZ=America/Los_Angeles date +%u)" = "7" ] || exit 0
[ "$(TZ=America/Los_Angeles date +%H:%M)" = "20:00" ] || exit 0
[ -s "$image_file" ] || { echo "equity_scanner_missing_production_image"; exit 1; }

# Read only for the failure alert webhook; the refresh container gets no notification
# settings.
export EQUITY_SCANNER_NOTIFICATION_ENV="${EQUITY_SCANNER_NOTIFICATION_ENV:-/opt/butterflyguy/.env}"

exec flock -n "$lock_file" sh -c '
  cd "$1"
  EQUITY_SCANNER_IMAGE="$(sed -n "1p" "$2")" \
  EQUITY_SCANNER_SECRET_ENV="$1/secrets/gateway.env" \
  EQUITY_SCANNER_GATEWAY_URL="$3" \
  EQUITY_SCANNER_SERVICE=refresh-universes \
  python3 "$1/src/equity_scanner/production_job.py"
' sh "$scanner_root" "$image_file" "$gateway_url"
