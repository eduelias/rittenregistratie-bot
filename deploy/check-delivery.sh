#!/usr/bin/env bash
# One-shot: is WhatsApp delivery working yet? Prints SENT or the blocking error.
set -euo pipefail
cd "$(dirname "$0")/.."
AT=$(grep '^RIT_WHATSAPP_TOKEN=' .env | cut -d= -f2-)
PID=$(grep '^RIT_WHATSAPP_PHONE_NUMBER_ID=' .env | cut -d= -f2-)
TO="${1:-31618337245}"
curl -s -X POST "https://graph.facebook.com/v21.0/${PID}/messages" \
  -H "Authorization: Bearer ${AT}" -H "Content-Type: application/json" \
  -d "{\"messaging_product\":\"whatsapp\",\"to\":\"${TO}\",\"type\":\"text\",\"text\":{\"body\":\"delivery check ✅\"}}" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);e=d.get("error");print("✅ SENT — delivery works!" if not e else f"⏳ blocked {e[\"code\"]}: {e[\"message\"]}")'
