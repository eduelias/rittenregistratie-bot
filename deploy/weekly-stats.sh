#!/bin/sh
# Weekly statistics for the owner: the analytics report, sent on WhatsApp
# through famulus on this host (the owner's own assistant, so it is not
# subject to the bot's 24-hour window). Run by rittenregistratie-stats.timer.
set -eu
cd "$(dirname "$0")/.."
owner=$(grep -m1 '^RIT_ADMIN_NUMBERS=' .env | cut -d= -f2- | cut -d, -f1 | tr -cd '0-9')
[ -n "$owner" ] || { echo "no RIT_ADMIN_NUMBERS in .env" >&2; exit 1; }
.venv/bin/python -m rittenregistratie.analytics report --days 7 |
  docker exec -i famulus python3 -c \
    "import asyncio,sys; from famulus import wa; asyncio.run(wa.send_text(sys.argv[1], sys.stdin.read()))" \
    "$owner"
