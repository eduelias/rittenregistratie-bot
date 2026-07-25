# LinkedIn post (draft)

Use `docs/assets/banner.svg` as the image (export to PNG 1200×630 for LinkedIn).

---

🚗📱 I open-sourced **rittenregistratie-bot** — log your car trips over WhatsApp.

Send one message — `145230 Office` — and it appends an audit-ready row to a
Belastingdienst-compliant Excel trip log, then replies with a summary. It runs on
a Raspberry Pi, uses the official WhatsApp Cloud API, and has a clean plugin
system.

Why I built it: keeping a watertight *rittenregistratie* for the Dutch
"Verklaring geen privégebruik auto" (≤500 private km/year) is tedious. This makes
it a 5-second WhatsApp message.

✅ Multi-car (by phone number)
✅ Private/business + 500 km rule awareness
✅ Asks for an address (or a shared location) when it doesn't know a place
✅ Self-hosted, MIT-licensed, tests + CI

The core never fabricates or reclassifies trips — it just records what you send.

Code: https://github.com/eduelias/rittenregistratie-bot

#opensource #Python #RaspberryPi #WhatsApp #Belastingdienst #selfhosted
