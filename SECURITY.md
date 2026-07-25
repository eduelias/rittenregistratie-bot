# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security vulnerabilities.

Instead, report privately via GitHub's
[Private Vulnerability Reporting](https://github.com/eduelias/rittenregistratie-bot/security/advisories/new)
(Security tab → Report a vulnerability), or contact the maintainer directly.

Include:

- A description of the vulnerability and its impact.
- Steps to reproduce (proof of concept if possible).
- Affected version/commit.

We aim to acknowledge reports within a few days and to address confirmed issues
promptly.

## Handling secrets

This project talks to the WhatsApp Cloud API and (optionally) Google APIs. Keep
these out of the repository:

- `.env` (contains `RIT_WHATSAPP_TOKEN`, `RIT_WHATSAPP_APP_SECRET`, etc.) — it is
  gitignored; never commit it.
- `config/cars.yaml` (contains personal phone numbers) — gitignored; ship
  `config/cars.yaml.example` instead.
- Access tokens, app secrets, and verification tokens must never appear in code,
  issues, or pull requests.

If you believe a secret was committed, rotate it immediately (regenerate the
token / app secret in the Meta App Dashboard) and open a private report.

## Scope

The bot verifies inbound webhooks using the `X-Hub-Signature-256` header and an
allow-list of sender numbers (via `config/cars.yaml`). When self-hosting, expose
it over HTTPS (e.g. Cloudflare Tunnel) and keep your host patched.
