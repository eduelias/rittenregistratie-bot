#!/usr/bin/env bash
# Monitor WhatsApp business verification + delivery capability.
#
# Reads the business verification status (if BUSINESS_ID is known) and probes a
# real send to detect when error 131037 clears. Announces the moment either the
# verification completes or delivery starts working.
#
# Usage:
#   ./deploy/monitor-verification.sh [interval_seconds] [to_number] [business_id]
set -euo pipefail
cd "$(dirname "$0")/.."

INTERVAL="${1:-300}"
TO="${2:-31618337245}"
BUSINESS_ID="${3:-}"

AT=$(grep '^RIT_WHATSAPP_TOKEN=' .env | cut -d= -f2-)
PID=$(grep '^RIT_WHATSAPP_PHONE_NUMBER_ID=' .env | cut -d= -f2-)

echo "Monitoring every ${INTERVAL}s. Recipient=${TO} PhoneID=${PID} BusinessID=${BUSINESS_ID:-<unknown>}"
echo "Announces when verification completes or 131037 clears. Ctrl-C to stop."

while true; do
  ts=$(date '+%Y-%m-%d %H:%M:%S')

  if [ -n "$BUSINESS_ID" ]; then
    vstat=$(curl -s "https://graph.facebook.com/v23.0/${BUSINESS_ID}?fields=verification_status,name" \
      -H "Authorization: Bearer ${AT}" \
      | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("verification_status", d.get("error",{}).get("message","?")))' 2>/dev/null || echo "?")
    echo "[$ts] business verification_status: ${vstat}"
    if [ "$vstat" = "verified" ]; then
      echo "[$ts] ✅ BUSINESS VERIFIED."
    fi
  fi

  resp=$(curl -s -X POST "https://graph.facebook.com/v23.0/${PID}/messages" \
    -H "Authorization: Bearer ${AT}" -H "Content-Type: application/json" \
    -d "{\"messaging_product\":\"whatsapp\",\"to\":\"${TO}\",\"type\":\"text\",\"text\":{\"body\":\"✅ Verified! Your trip-logger bot can now reply. Send 'ping' to test, or '<odometer> <destination>' to log a trip.\"}}")
  code=$(printf '%s' "$resp" | python3 -c 'import sys,json;d=json.load(sys.stdin);e=d.get("error");print(e.get("code") if e else "OK")' 2>/dev/null || echo "PARSE_ERR")

  if [ "$code" = "OK" ]; then
    echo "[$ts] ✅ DELIVERY WORKS — confirmation message sent to ${TO}. Done."
    exit 0
  else
    echo "[$ts] ⏳ send blocked (code ${code})."
  fi
  sleep "${INTERVAL}"
done
