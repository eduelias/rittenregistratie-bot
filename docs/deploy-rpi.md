# Deploying on a Raspberry Pi (reference)

This is the concrete setup used on a Raspberry Pi 5 (Debian Bookworm, arm64),
running the bot as a systemd service behind a Cloudflare quick tunnel.

## 1. Clone + install

```bash
mkdir -p ~/reps/du7/trip-logger && cd ~/reps/du7/trip-logger
git clone https://github.com/eduelias/rittenregistratie-bot.git
cd rittenregistratie-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## 2. Configure

```bash
cp config/.env.example .env && chmod 600 .env
# Edit .env: RIT_WHATSAPP_TOKEN, RIT_WHATSAPP_PHONE_NUMBER_ID,
#            RIT_WHATSAPP_APP_SECRET, RIT_WHATSAPP_VERIFY_TOKEN
# Edit config/cars.yaml: map your WhatsApp number -> car, set seed odometer.
mkdir -p data
```

## 3. Bot service

```bash
sudo cp deploy/rittenregistratie.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rittenregistratie.service
curl -s http://127.0.0.1:8000/health   # {"status":"ok"}
```

## 4. Public webhook (Cloudflare quick tunnel)

For a stable hostname use a named tunnel (see `cloudflared.md`). For quick
testing, a quick tunnel gives a random `*.trycloudflare.com` URL that changes on
restart:

```bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /tmp/cloudflared && sudo install -m 0755 /tmp/cloudflared /usr/local/bin/cloudflared
sudo cp deploy/cloudflared-quick.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-quick.service
# Find the public URL:
journalctl -u cloudflared-quick.service | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1
```

Register `https://<url>/webhook` + your verify token in Meta and subscribe to the
`messages` field.

## 5. Reset / start fresh

To start real logging from the car's current odometer:

```bash
# set seed_odometer (and seed_address) in config/cars.yaml, then:
rm -f data/state-<car>.json data/trips-<car>-*.xlsx
sudo systemctl restart rittenregistratie.service
```

## Useful

```bash
journalctl -u rittenregistratie -f              # live logs
sudo systemctl restart rittenregistratie        # reload after .env/config edits
```

> Test numbers only deliver to verified recipients. Add your number in the
> WhatsApp API Setup / dev console recipient list before expecting replies.
