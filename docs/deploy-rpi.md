# Deploying on a Raspberry Pi (reference)

The setup used on du7, a Raspberry Pi 5 (Debian Bookworm, arm64): the bot as a
systemd service, published by Caddy, with nightly backups. Paths below are
du7's; adjust `User`, `Group` and the paths in `deploy/*.service` for another host.

## 1. Clone and install

```bash
mkdir -p ~/reps/du7/trip-logger && cd ~/reps/du7/trip-logger
git clone https://github.com/eduelias/rittenregistratie-bot.git
cd rittenregistratie-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e . -c requirements.lock      # the versions that run in production
```

## 2. Configure

```bash
cp config/.env.example .env && chmod 600 .env
# Edit .env: RIT_WHATSAPP_TOKEN, RIT_WHATSAPP_PHONE_NUMBER_ID,
#            RIT_WHATSAPP_APP_SECRET, RIT_WHATSAPP_VERIFY_TOKEN
# Edit config/cars.yaml: map your WhatsApp number -> car, set seed odometer.
mkdir -p data
```

Without `RIT_WHATSAPP_APP_SECRET` the bot refuses every webhook, because it
cannot check that Meta sent it. On du7 the `.env` is rendered from 1Password
(`op-env render` on the Mac, then copied over); do not keep copies of it.

## 3. Services and timers

```bash
sudo cp deploy/rittenregistratie.service deploy/rittenregistratie-*.service \
        deploy/rittenregistratie-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rittenregistratie.service rittenregistratie-backup.timer \
        rittenregistratie-retention.timer rittenregistratie-stats.timer
curl -s http://127.0.0.1:8000/health   # {"status":"ok"}
```

| Unit | When | What |
|---|---|---|
| `rittenregistratie.service` | always | the bot and the website, on port 8000 |
| `rittenregistratie-backup.timer` | 03:15 | `data/` and `config/` to `~/backups/rittenregistratie` and the NAS disk, 30 days |
| `rittenregistratie-retention.timer` | 04:15 | deletes a former subscriber's trips 90 days after access ended |
| `rittenregistratie-stats.timer` | Monday 08:30 | the weekly report to the owner |

The service binds `0.0.0.0` because Home Assistant posts car trips to
`/hooks/...` over the LAN. It runs with `ProtectSystem=strict` and
`ProtectHome=read-only`: it can write only `data/` and `config/` (checked on du7
by trying to write elsewhere from inside the service's namespace).

Logs go to journald. The privacy statement promises at most 30 days, so the
host needs `MaxRetentionSec=30day` (du7: `/etc/systemd/journald.conf.d/30-retention.conf`).

## 4. Public address

Caddy terminates TLS and proxies to `127.0.0.1:8000`: paste
`deploy/Caddyfile.rrbot` into the host's Caddyfile and reload Caddy. It keeps
`/hooks/*` off the internet. Publish port 8000 through nothing else (no tunnel,
no Tailscale Funnel): that would bypass the filter.

Register `https://<host>/webhook` and your verify token in Meta, and subscribe
to the `messages` field.

## 5. Restore from a backup

```bash
sudo systemctl stop rittenregistratie
.venv/bin/python -m rittenregistratie.backup restore <archive.tar.gz> --to /tmp/restore
# look at /tmp/restore/data and /tmp/restore/config, then put them in place:
cp -a /tmp/restore/data/. data/ && cp -a /tmp/restore/config/. config/
sudo systemctl start rittenregistratie
```

Both backup copies are in the same house as the Pi. There is no off-site copy
yet (the owner's decision, 2026-11-20): a fire or theft takes the record.

## 6. Reset / start fresh

To start real logging from the car's current odometer:

```bash
# set seed_odometer (and seed_address) in config/cars.yaml, then:
rm -f data/state-<car>.json
sudo systemctl restart rittenregistratie.service
```

## Useful

```bash
journalctl -u rittenregistratie -f              # live logs
sudo systemctl restart rittenregistratie        # reload after .env/config edits
```

> Test numbers only deliver to verified recipients. Add your number in the
> WhatsApp API Setup / dev console recipient list before expecting replies.
