# rittenregistratie-bot

<p align="center">
  <img src="docs/assets/banner.svg" alt="rittenregistratie-bot — log your car trips over WhatsApp" width="100%">
</p>

<p align="center">
  <a href="https://github.com/eduelias/rittenregistratie-bot/actions/workflows/ci.yml"><img src="https://github.com/eduelias/rittenregistratie-bot/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/runs%20on-Raspberry%20Pi-c51a4a.svg" alt="Runs on Raspberry Pi">
</p>

A self-hosted WhatsApp bot that keeps a **Belastingdienst-compliant trip log
(rittenregistratie)** for one or more cars. You send a WhatsApp message with your
odometer reading and destination; it records the trip in an append-only ledger
and, whenever you ask, sends you the audit-ready per-year Excel file.

Designed to help holders of the Dutch
[*Verklaring geen privégebruik auto*](https://www.belastingdienst.nl/wps/wcm/connect/nl/personeel-en-loon/content/verklaring-geen-privegebruik-auto-aanvragen-wijzigen-intrekken)
keep the watertight trip administration the Belastingdienst requires, and to run
comfortably on a Raspberry Pi.

> **Disclaimer**
> This software helps you *record* trips. It is **not tax advice** and does not
> guarantee acceptance by the Belastingdienst. You are solely responsible for
> the correctness and completeness of your administration. The open-source core
> never fabricates, invents, or reclassifies trips.

## Features

- **Log by WhatsApp** — one message per trip: `145230 Office`.
- **Belastingdienst-compliant Excel on request** — send `excel` and the bot
  replies with the per-year workbook (date, begin/end odometer, begin/end
  address, route, private/business) as a WhatsApp document.
- **Append-only ledger as the only store** — every trip is one JSON line,
  written once and never modified; spreadsheets are views generated from it.
- **Event bus for plugins** — the core emits `export.pre_generate` and
  `export.post_generate`; a plugin may transform the data before the workbook
  is written. Without plugins the export is exactly what you typed.
- **Multi-car** — sender phone number selects the car; separate logs per car.
- **Private/business** classification with the 500 km/year cap awareness.
- **Ask-for-address** — unknown destinations prompt for an address, the name of
  a known location, or a shared location (reverse-geocoded). Reply `cancel` to
  drop the trip. Learned addresses are kept in `data/locations-learned.yaml`.
- **Case-insensitive names with aliases** — `Home`, `home` and `HOME` are one
  place; a location whose address is another location's name resolves to that
  address, so every alias yields one canonical address string.
- **Thumbs-up acknowledgement** — set `RIT_REPLY_MODE=reaction` and the bot
  reacts 👍 to your message instead of replying, sending text only when it
  needs something or has extra information.
- **Plugin system** — swap odometer source, trajectory provider and private-cap
  reporting via entry points; the core stays a faithful recorder.
- **Self-hosted** — FastAPI + WhatsApp Cloud API, exposed via Cloudflare Tunnel,
  runs great on a Raspberry Pi.
- **`ping` → `pong`** connectivity check built in.

## How it works

Send a WhatsApp message:

```
145230 Office
145230 Office private
145230 Kantoor Amsterdam #private -- lunch with client
```

- First value = current odometer reading (end of trip).
- Rest = destination (a known name from `config/locations.yaml`, or free text).
- `private` / `#private` marks a private trip.
- Text after `--` or `|` is stored as a note.

Origin and start odometer are taken from the previous trip (seeded once via
config for the very first trip).

### Unknown destinations

If the destination is not a known location the bot asks for its address and
waits. Your next message may be:

- a street address, e.g. `Waldorpstraat 3, Den Haag` — learned and used;
- the name of a known location, e.g. `office` — its address is reused;
- a shared WhatsApp location — reverse-geocoded when a Google key is set;
- `cancel` — the pending trip is dropped and nothing is logged;
- a new trip, e.g. `20672 spg` — the pending trip is dropped (it had no
  address) and the new one is processed. A reply that looks like an odometer
  reading is never stored as an address.

Learned addresses are written to `data/locations-learned.yaml`, so the
committed `config/locations.yaml` stays a clean seed file. Names are matched
case-insensitively and an address that is itself a known name is followed.

### Replies

`RIT_REPLY_MODE` controls how a logged trip is acknowledged:

| Mode       | Logged trip                         | Prompts, errors, `pong` |
|------------|-------------------------------------|-------------------------|
| `text`     | summary message (default)           | text                    |
| `reaction` | 👍 on your message; text only if there is extra info (learned address, route deviation, cap warning) | text |
| `both`     | 👍 and the summary message          | text                    |

If a reaction cannot be delivered the bot falls back to the text summary.

## Multi-car (identify car by phone number)

Each WhatsApp sender number is mapped to a car in `config/cars.yaml`. The
sender's number selects the car, so several people/cars can share one bot:

```yaml
default_car:
  label: "Company car"
  seed_address: "Home"
  seed_odometer: 145000
  phones: ["31612345678"]
van:
  label: "Delivery van"
  seed_address: "Depot"
  seed_odometer: 302000
  phones: ["31698765432", "31611112222"]
```

- Only numbers listed in `cars.yaml` are accepted; unknown numbers are ignored.
- Each car has its **own** state file (`state-<car>.json`) and Excel logs
  (`trips-<car>-<year>.xlsx`), keeping every administration separate.
- A phone number may belong to exactly one car.

## Onboarding new users

New people can join by simply **messaging the bot**. Their request is queued for
an **admin** (configured via `RIT_ADMIN_NUMBERS`) to approve — no config editing
needed.

1. A new number messages the bot → it replies that a request was sent, and
   notifies the admin(s).
2. An admin replies with WhatsApp commands:

   ```
   pending                                   # list join requests
   approve <number> <label> <seed_odo> [address]   # register a new user/car
   deny <number>                             # reject a request
   list                                      # list registered users
   remove <number|car_id>                    # remove a user
   help                                      # show admin commands
   ```

   e.g. `approve 31612345678 Alice Golf 45000 Delft`

3. The bot registers the number as a new car in `cars.yaml`, seeds its odometer,
   and **welcomes the new user**. They can start logging trips immediately.

Each user has their **own spreadsheet** (`trips-<car_id>-<year>.xlsx`) and state.
The number of users is capped by `RIT_MAX_USERS` (default 5). Removing a user
keeps their existing trip logs on disk.

Set `RIT_ONBOARDING_ENABLED=false` to silently ignore unregistered numbers.

## Storage and export

Trips are stored **only** in the append-only ledger
`data/raw-ledger-<car>.jsonl`, exactly as you reported them. No spreadsheet is
written when you log a trip.

Ask for the workbook over WhatsApp:

```
excel              # current year, your car
excel 2025         # a given year
excel all          # one file per year with trips
excel van 2025     # admins only: another car
```

The bot reacts ⏳ (or replies "Generating…" in text mode), builds the workbook
in the background, and sends it as a document. The file is also kept at
`data/exports/trips-<car>-<year>.xlsx` and replaced on the next export.

The export pipeline is where plugins may act (see [Plugin system](#plugin-system)):

1. read the ledger rows for the car and year;
2. emit `export.pre_generate` with the rows as plain JSON objects and take back
   what the handlers return — with no plugin installed, the rows unchanged;
3. validate the result (required fields, integer odometers, ISO timestamps, a
   non-decreasing odometer chain); a bad result aborts the export with an error
   message rather than sending a partial file;
4. write the workbook and emit `export.post_generate` with its path.

## Excel schema (exactly the Belastingdienst per-trip fields)

Each trip is one row with exactly the fields the Belastingdienst requires —
nothing more, nothing less:

```
Datum | Beginstand | Eindstand | Vertrekadres | Aankomstadres |
Route | Privé/zakelijk | Privé-omrijkilometers
```

- **Route** is filled *only* when the driven route is not the most usual one
  (empty otherwise), as the rules specify.
- A there-and-back visit is recorded as **2 trips** (two rows).

One file per car per year: `trips-<car>-<year>.xlsx`. See
`docs/belastingdienst.md`.

## Immutable raw ledger

Every trip is appended, exactly as you reported it, to the **append-only**
ledger at `data/raw-ledger-<car>.jsonl` (one JSON object per line). This ledger
is never modified after writing and is the single source of truth.

Besides the WhatsApp `excel` command, the same export pipeline can be run from
the command line (`python -m rittenregistratie.export <car> [year]`, see
`docs/plugins.md`), and a pristine spreadsheet bypassing all plugins can be
rebuilt with:

```bash
python -m rittenregistratie.rebuild <car_id> [--out ./restore]
```

## Plugin system

Extension points are discovered via setuptools entry points, so other packages
can add or override behaviour without changing the core. Three select an
implementation by name; the fourth, `rittenregistratie.events`, lets a package
subscribe to the events the core emits (today: `export.pre_generate` and
`export.post_generate`). Select event plugins with `RIT_EVENT_PLUGINS`
(`*` all installed, empty none, or a list of names).

| Group                        | Default (shipped)      | Purpose                              |
|------------------------------|------------------------|--------------------------------------|
| `rittenregistratie.odometer` | `whatsapp_manual`      | where odometer readings come from    |
| `rittenregistratie.trajectory`| `maps_link` / `google`| resolve the route between addresses  |
| `rittenregistratie.privatecap`| `warn`                | report on the 500 km/yr private cap  |

The core is a **faithful recorder**: it stores each trip exactly as reported
(the distance is always the real odometer difference) and never fabricates,
invents, or reclassifies trips. The shipped `warn` plugin only reports a message.

### Per-user private-cap plugin

By default one private-cap plugin applies to everyone. You can apply a
**different** cap plugin to specific numbers only:

```
RIT_PRIVATE_CAP_PLUGIN=warn                 # default for everyone
RIT_PRIVATE_CAP_PLUGIN_OVERRIDE=my_plugin   # a plugin you install...
RIT_PRIVATE_CAP_OVERRIDE_NUMBERS=31612345678,31698765432  # ...only for these
```

A car uses the override plugin if any of its numbers is on the list; all other
cars keep the default. Leave the override empty to disable this. You are
responsible for the behaviour of any plugin you install.

See `docs/plugins.md`.

## Architecture

<p align="center">
  <img src="docs/assets/architecture.svg" alt="rittenregistratie-bot architecture" width="100%">
</p>

Your WhatsApp message → Meta Cloud API → Cloudflare Tunnel → the FastAPI webhook
on your Raspberry Pi. The parser turns it into a trip, the core Engine resolves
the car, updates state and appends a compliant Excel row, then replies. Plugin
entry points let you swap the odometer source, trajectory provider and
private-cap reporting without touching the core.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp config/.env.example .env    # then edit
pytest -q
uvicorn rittenregistratie.main:app --host 0.0.0.0 --port 8000
```

Then expose port 8000 to Meta with Cloudflare Tunnel (`deploy/cloudflared.md`)
and register the webhook. Run it as a service with `deploy/rittenregistratie.service`.

### Run as a systemd service

`deploy/rittenregistratie.service` runs uvicorn on `127.0.0.1:8000` as your
user. Adjust `User`, `Group` and the paths to match your host, then:

```bash
sudo cp deploy/rittenregistratie.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rittenregistratie.service
systemctl status rittenregistratie.service
curl -s http://127.0.0.1:8000/health   # {"status":"ok"}
```

## License

MIT — see [`LICENSE`](LICENSE).

## Contributing

Contributions are welcome! Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
our [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). For security issues, see
[`SECURITY.md`](SECURITY.md).
