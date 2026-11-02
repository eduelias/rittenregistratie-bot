# rittenregistratie-bot

Self-hosted WhatsApp bot keeping a Belastingdienst-compliant Dutch trip log
(rittenregistratie) for one or more cars. Users text an odometer reading +
destination; the bot appends it to an immutable ledger and generates an
audit-ready per-year Excel workbook on request (`excel`). A faithful
recorder — never invents or reclassifies trips. Not tax advice.

## Where it runs

Pi `du7` (192.168.2.68), systemd service `rittenregistratie.service`
(uvicorn on `127.0.0.1:8000`, exposed via Cloudflare Tunnel). **See the
du7-deploy skill** for the actual procedure — roughly:

```bash
git pull && sudo -n systemctl restart rittenregistratie
```

Related but out of scope here: `../rittenregistratie-plugins-private` (own
pyproject/tests/deploy, registers plugins against these entry points) and
`../STATUS-whatsapp-debug.md` (dated debug log, historical, not live state).

## Stack

- Python >=3.11, `setuptools` build backend, `src/` layout, package
  `rittenregistratie`.
- Deps: `fastapi`, `uvicorn[standard]`, `httpx`, `openpyxl`, `pydantic(-settings)`,
  `PyYAML`. Dev extras: `pytest`, `pytest-asyncio`.
- Plugins via setuptools entry points: `rittenregistratie.odometer`
  (`whatsapp_manual` default, `mercedes_stub`), `rittenregistratie.trajectory`
  (`maps_link` default, `google`, `manual`), `rittenregistratie.privatecap`
  (`warn` default), `rittenregistratie.events` (no default; subscribers
  register a `register(bus)` callable).

## Setup / commands

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config/.env.example .env      # WhatsApp creds, admin numbers, etc.
pytest -q                        # tests/ (pythonpath=src, asyncio_mode=auto)
uvicorn rittenregistratie.main:app --host 0.0.0.0 --port 8000
python -m rittenregistratie.export <car_id> [year]
python -m rittenregistratie.rebuild <car_id> [--out ./restore]   # plugin-free rebuild
```

No ruff config here (unlike the private-plugins sibling) — CI runs pytest only.

## Architecture

- `whatsapp.py` — Meta Cloud API client + inbound signature verification.
- `parser.py` — message → odometer + destination + private flag + note.
- `engine.py` — resolves active car, continues the odometer chain, resolves
  destinations, applies private/business + the 500 km/yr cap, appends to
  ledger, replies.
- `raw_ledger.py` — the **only** store: `data/raw-ledger-<car_id>.jsonl`,
  append-only, never rewritten; spreadsheets are always regenerated views.
- `export.py`/`excel_writer.py` — read ledger rows → emit `export.pre_generate`
  (plugins may transform) → validate (fields, integer odometers, ISO
  timestamps, non-decreasing odometer) → write workbook → emit
  `export.post_generate`.
- `events.py` — `EventBus`: sync handlers in registration order, each may
  transform the payload.
- `cars.py` — multi-car/phone mapping (`config/cars.yaml`), per-car
  ledger/state/plugin selection.
- `onboarding.py` — join requests + admin commands (`pending`, `approve`,
  `assign`, `unassign`, `deny`, `list`, `remove`).
- `billing.py` + `site.py` (+ `site/*.html`) — rrbot.nl landing page and paid
  signup through Mollie: first payment → subscription → car with
  `subscription:` in cars.yaml. See `docs/subscriptions.md`. Also the SEO side:
  `robots.txt`, `sitemap.xml` (`SITEMAP`), an HTML 404 (`install_not_found`),
  the guide pages in `GUIDES` and the free Excel template
  (`excel_writer.template_workbook`). The guides make tax claims with a
  belastingdienst.nl source per rule: re-check them when the rules change.
  English site under `/en/` (`site/en/`, `EN_GUIDES`): landing, requirements
  guide, terms and privacy (translations; the Dutch versions prevail). `PAIRS`
  maps each Dutch page to its English one for hreflang, the sitemap
  alternates and the NL/EN switch. A signup from `/en/` stores `lang=en`: the
  bot speaks English to that number and Mollie's checkout is `en_US`.
- `i18n.py` — every bot text in Dutch and English (`CATALOG`), language per
  number (`data/languages.json`, `taal`/`language` command), default
  `RIT_DEFAULT_LANGUAGE=nl`. Use `t("key", ...)`, never a literal reply. Tests
  run in English unless the module sets `BOT_LANGUAGE = "nl"` (conftest.py).
- `analytics.py` — cookieless daily page counts (`data/analytics.json`) and the
  subscription funnel; admin `stats [days]`, weekly report via famulus.
- `invoices.py` — a VAT invoice (PDF, art. 35a Wet OB) for every paid
  subscription payment, numbered `2026-0001` without gaps (`TEST-` series for
  test-mode payments), stored in `data/invoices/` and sent on WhatsApp (the
  `invoice_ready` template when the window is closed). Commands `factuur`,
  `facturen`, `factuurgegevens`, `bedrijfsnaam`, `factuuradres`. Never change
  an issued invoice.
- `wa_health.py` — every 6 hours the number's quality rating and messaging
  limit into `data/whatsapp-health.json`; admins are alerted on a drop or a
  limit change; the weekly report shows the last reading.
- `retention.py` — deletes former subscribers' trip data after
  `RIT_RETENTION_DAYS`; daily timer in `deploy/`.

## Config / secrets (`config.py`, prefix `RIT_`, `.env` not committed)

WhatsApp: `RIT_WHATSAPP_TOKEN/PHONE_NUMBER_ID/APP_SECRET/VERIFY_TOKEN/GRAPH_VERSION`.
Access: `RIT_ALLOWED_SENDER`, `RIT_ADMIN_NUMBERS`, `RIT_ONBOARDING_ENABLED`,
`RIT_MAX_USERS`. Behaviour: `RIT_REPLY_MODE`, `RIT_GOOGLE_MAPS_API_KEY`.
Plugins: `RIT_ODOMETER_SOURCE`, `RIT_TRAJECTORY_PROVIDER`,
`RIT_PRIVATE_CAP_PLUGIN(_OVERRIDE)`, `RIT_PRIVATE_CAP_OVERRIDE_NUMBERS`,
`RIT_EVENT_PLUGINS`. Compliance/seed: `RIT_PRIVATE_CAP_KM`,
`RIT_SEED_ADDRESS/ODOMETER`. Paths: `data/` (ledgers, state, exports, learned
locations) vs `config/` (committed seed `cars.yaml`/`locations.yaml`/`routes.yaml`).
`data/locations-learned.yaml` holds chat-learned addresses, kept out of the
committed config.

## Testing

`tests/test_*.py`, `pytest` + `pytest-asyncio`. Fixtures build an isolated
`Settings` pointed at `tmp_path` with inline YAML (see `test_engine.py`), so
tests never touch real `config/`/`data/`. Export tests read back workbooks
with `openpyxl`.

## Gotchas

- Raw ledger is append-only — never edit a written line; correct by appending.
- Only numbers in `cars.yaml` are served; others get onboarding or are ignored.
- A phone with multiple cars must disambiguate (`car <id>` or prefix) or the
  bot logs nothing.
- A reply that looks like an odometer reading is never stored as a learned
  address.
- A car with `subscription:` (a paying customer) never loads plugins outside
  `RIT_SUBSCRIBER_EVENT_PLUGINS`, always uses `RIT_SUBSCRIBER_CAP_PLUGIN`, and
  has its own learned-places file. Keep the private plugins away from them.
- WhatsApp Business setup issues (display-name/verification) are separate
  from app bugs — see the sibling `STATUS-whatsapp-debug.md` for a past case.
