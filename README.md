# rittenregistratie-bot

A self-hosted WhatsApp bot that keeps a **Belastingdienst-compliant trip log
(rittenregistratie)** for a single car. You send a WhatsApp message with your
odometer reading and destination; it appends an audit-ready row to a per-year
Excel file and replies with a summary.

Designed to help holders of the Dutch
[*Verklaring geen privégebruik auto*](https://www.belastingdienst.nl/wps/wcm/connect/nl/personeel-en-loon/content/verklaring-geen-privegebruik-auto-aanvragen-wijzigen-intrekken)
keep the watertight trip administration the Belastingdienst requires, and to run
comfortably on a Raspberry Pi 5.

> **Disclaimer**
> This software helps you *record* trips. It is **not tax advice** and does not
> guarantee acceptance by the Belastingdienst. You are solely responsible for
> the correctness and completeness of your administration. The open-source core
> never fabricates, invents, or reclassifies trips.

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

## Excel schema (audit fields)

```
Date | Time | Type | StartAddress | EndAddress | StartOdo | EndOdo |
TripKm | Route | DeviationNote | Source | PrivateKmYTD
```

One file per car per year: `trips-<car>-<year>.xlsx`.

These map to the mandatory Belastingdienst fields: date, begin/end odometer,
begin/end address, the driven route (with a note when it deviates from the usual
route), and the business/private character. See `docs/belastingdienst.md`.

## Plugin system

Four extension points are discovered via setuptools entry points, so other
packages can add or override behaviour without changing the core:

| Group                        | Default (shipped)      | Purpose                              |
|------------------------------|------------------------|--------------------------------------|
| `rittenregistratie.odometer` | `whatsapp_manual`      | where odometer readings come from    |
| `rittenregistratie.trajectory`| `maps_link` / `google`| resolve the route between addresses  |
| `rittenregistratie.delta`    | `noop`                 | handle excess km vs. expected route  |
| `rittenregistratie.privatecap`| `warn`                | react to the 500 km/yr private cap   |

The shipped defaults are **inert and safe**: `noop` never generates trips and
`warn` never reclassifies them. AI-based trip allocation and private-use
reallocation are intentionally **not** part of this open-source core; they live
in separate plugin packages.

See `docs/plugins.md`.

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

MIT — see `LICENSE`.
