# Exposing the webhook with Cloudflare Tunnel

Meta must reach your Raspberry Pi over public HTTPS. Cloudflare Tunnel does this
without opening router ports or needing a static IP.

## Install

```bash
# Raspberry Pi OS (arm64)
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
```

## Named tunnel (stable hostname — recommended)

You need a domain on Cloudflare.

```bash
cloudflared tunnel login
cloudflared tunnel create rittenregistratie
# Map a hostname to the local bot:
cloudflared tunnel route dns rittenregistratie trips.example.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: rittenregistratie
credentials-file: /home/pi/.cloudflared/<TUNNEL-ID>.json
ingress:
  - hostname: trips.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Run it as a service:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Your webhook URL for Meta is: `https://trips.example.com/webhook`

## Quick tunnel (no domain, URL changes on restart — testing only)

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

## Register the webhook in Meta

1. Meta app → WhatsApp → Configuration → Webhook.
2. Callback URL: `https://trips.example.com/webhook`
3. Verify token: the value of `RIT_WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the `messages` field.
