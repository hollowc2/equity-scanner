#!/bin/sh
set -eu

phase=${1:?usage: capture_parity_gateway_health.sh before|after}
case "$phase" in
  before|after) ;;
  *) echo "parity_health_invalid_phase"; exit 2 ;;
esac

scanner_root=${EQUITY_SCANNER_ROOT:-/opt/equity-scanner}
run_date=$(TZ=America/New_York date +%F)
[ "$run_date" = "${EQUITY_SCANNER_PARITY_DATE:?set approved parity date}" ] || exit 0

output_root="$scanner_root/parity/$run_date/output"
mkdir -p "$output_root"
metrics_tmp=$(mktemp)
trap 'rm -f "$metrics_tmp"' EXIT HUP INT TERM

curl --silent --show-error --fail http://127.0.0.1:8011/health \
  > "$output_root/gateway-health-$phase.json"
curl --silent --show-error --fail http://127.0.0.1:8011/ready \
  > "$output_root/gateway-ready-$phase.json"
curl --silent --show-error --fail http://127.0.0.1:8011/metrics > "$metrics_tmp"

awk '
  /^(gateway_client_requests_total|gateway_client_request_latency_seconds(_bucket|_sum|_count)?|gateway_order_book_subscriber_drops_total|schwab_gateway_scheduler_capacity_rejections_total|schwab_gateway_scheduler_queue_wait_seconds(_bucket|_sum|_count)?|schwab_gateway_scheduler_queue_wait_timeouts_total|schwab_gateway_scheduler_upstream_timeouts_total|schwab_gateway_token_state)(\{| )/ { print }
' "$metrics_tmp" > "$output_root/gateway-metrics-$phase.prom"
[ -s "$output_root/gateway-metrics-$phase.prom" ]

{
  printf "captured_at_utc=%s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf "gateway_restart_count="
  docker inspect --format '{{.RestartCount}}' schwab_gateway_live
  printf "gateway_recent_error_count="
  docker logs --since 1h schwab_gateway_live 2>&1 \
    | grep -Eic 'error|exception|traceback' || true
  docker ps --format '{{.Names}}|{{.Image}}|{{.Status}}' \
    | grep -E '^(schwab_gateway_live|butterfly_(spx|ndx|xsp)_app)\|'
  printf "candidate_container_count="
  docker ps -aq --filter label=com.docker.compose.project=equity_scanner_candidate \
    | wc -l
} > "$output_root/gateway-runtime-$phase.txt"

chmod 0600 \
  "$output_root/gateway-health-$phase.json" \
  "$output_root/gateway-ready-$phase.json" \
  "$output_root/gateway-metrics-$phase.prom" \
  "$output_root/gateway-runtime-$phase.txt"
