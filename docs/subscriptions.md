# Paid subscriptions (rrbot.nl)

The hosted bot sells one plan: one car, one WhatsApp number, **€4.95/month or
€49/year incl. VAT, 14 days free**. Payments go through **Mollie**. The website
runs on **rrbot.nl** and is served by the bot itself. A number is added
**automatically** once the first payment is paid.

Decided with the owner on 2026-11-01.

## Flow

```
rrbot.nl form ──POST /aanmelden──► Billing.start
                                     ├─ Mollie customer
                                     └─ first payment €0.01 (sequenceType=first) ──► Mollie checkout (iDEAL / card)
Mollie ──POST /billing/mollie/webhook (id=tr_…)──┐
customer ──GET /aanmelden/klaar?id=…─────────────┴► Billing.sync_payment  (fetches the payment; idempotent)
                                     paid ─► subscription (startDate = today + 14 days, €4.95 1 month | €49 12 months)
                                          ─► cars.yaml: new car {phones, event_plugins: [], cap_plugin: warn, subscription: <id>}
                                          ─► WhatsApp to admins; success page with a wa.me link ("start")
```

- **The webhook body is never trusted.** It carries only an id, and the bot
  fetches the payment from Mollie with its own key.
- **The return page syncs as well**, so signup also works without a reachable
  webhook (local test mode) and when the customer arrives before Mollie's call.
- **Subscriber messages over WhatsApp:**
  - `start`/`hallo`/`help`: Dutch instructions.
  - `abonnement`: status and next payment.
  - `opzeggen`: cancels in Mollie. Access continues until the day before the
    next charge, or until the trial ends. The car is removed from `cars.yaml`
    on the first message after that. The ledger stays on disk.
- **A failed or charged-back recurring payment** sends one WhatsApp to the
  admins. The number stays active and the owner decides (`remove <number>`).
- **Unregistered numbers** get a reply with the signup link. No join request
  is queued while billing is on.
- **When `RIT_MAX_USERS` is reached**, the site shows "vol" and refuses
  signups before anyone pays. A paid signup is always provisioned, even past
  the limit.
- **Signups** are kept in `data/signups.json`: name, email, phone, car, Mollie
  ids, and a short event trail. Mollie remains the record for money.

## Keeping the owner's setup private

Before this change, `approve` wrote cars with no `event_plugins`. Those cars
fell back to the global `RIT_EVENT_PLUGINS=reallocate`, so every approved
number would have had the private relocation plugin. Since this change:

| | Owner's car(s) | Subscriber car (`subscription:` set) |
|---|---|---|
| Event plugins | `event_plugins` in cars.yaml, else `RIT_EVENT_PLUGINS` | only names in `RIT_SUBSCRIBER_EVENT_PLUGINS` (default none), whatever cars.yaml or `.env` say |
| Cap plugin | `cap_plugin`, per-number override, or default | always `RIT_SUBSCRIBER_CAP_PLUGIN` (`warn`) |
| Places | `config/locations.yaml` + `data/locations-learned.yaml` (shared) | `data/locations-learned-<car>.yaml` only |

Places were shared before: a subscriber typing `home` would have got the
owner's home address in their tax log. Tests:
`tests/test_subscriber_isolation.py`.

A separate bot instance would isolate more strongly, because the private
package would not even be installed. It needs a second WhatsApp number. Worth
doing if the private plugins grow; not needed for this.

## Test mode, end to end

1. Put the Mollie **test** key (`test_…`) in 1Password, item `rittenregistratie-bot .env`, field
   `RIT_MOLLIE_API_KEY`.
2. Run the bot locally against a throwaway config dir with
   `RIT_PUBLIC_BASE_URL=http://127.0.0.1:8011` (no webhook is sent for a local
   URL; the return page syncs instead) and WhatsApp sending disabled.
3. Open the page, sign up, and in Mollie's test checkout pick **Paid**. The
   return page shows "Welkom" and `cars.yaml` has the new car with
   `event_plugins: []`. The Mollie test dashboard shows the customer, the
   €0.01 payment and the subscription starting in 14 days.
4. Run it again and pick **Failed**: nothing is added. Then send `opzeggen`
   from the new number (or call `Billing.cancel`) and check that the
   subscription is cancelled in the dashboard.

### Result, 2026-11-01 (local, real Mollie test mode)

| Case | Outcome |
|---|---|
| Signup, card test payment **Paid** | "Welkom" page; car `golf` in cars.yaml with `event_plugins: []`, `cap_plugin: warn`, `subscription:`; Mollie: valid card mandate, subscription €4.95 / 1 month starting today + 14 days |
| Subscriber messages | `start` gave the Dutch help; `45230 home` asked for an address (the owner's "home" was not used); the reply was learned into the car's own file; `privé` was recognised; `abonnement` read the live subscription |
| Signup (yearly), **Failed** | "niet gelukt" page; no car, no Mollie subscription; that number messaging the bot got the signup link |
| `opzeggen` | Mollie subscription `canceled`; access until the day before the first charge; admin notified; a second `opzeggen` is harmless |

Not covered by this run, but by unit tests: Mollie calling the webhook
(not possible with a local URL; it gets exercised for real on rrbot.nl),
recurring payment failures, and removal after `access_until`.

## Deploy (owner's OK needed for each step)

1. `du7/dns-zone`: `op-env run -- tofu apply` (adds the rrbot.nl zone).
   `tofu output rrbot_nameservers` → Squarespace › rrbot.nl › DNS › custom
   nameservers. In Cloudflare, click the verification mail for Email Routing
   if it asks.
2. Caddy: paste `deploy/Caddyfile.rrbot` into
   `/home/esaleh/dns-stack/caddy/Caddyfile`, then reload Caddy. Check
   `curl -sI https://rrbot.nl` once DNS is live.
3. 1Password: add `RIT_MOLLIE_API_KEY` (test key first). `op-env render` the
   `.env.tpl`, `pi_push` it, and check that `RIT_EVENT_PLUGINS` is now empty
   (the mercedes car keeps `reallocate` + `homeassistant` through cars.yaml).
4. `git pull` + `sudo -n systemctl restart rittenregistratie` (du7-deploy).
5. Meta app › WhatsApp › Configuration: webhook URL
   `https://rrbot.nl/webhook`, same verify token. Once messages arrive there,
   stop and disable `cloudflared-quick.service`.
6. Run a test-mode signup on https://rrbot.nl. Delete the test car with
   `remove <number>`.

## Still needed before taking real money

Legal and business:
- [ ] **KvK number and VAT number** in `RIT_COMPANY_LINE`. Prices are shown
      incl. VAT; if du7 uses the KOR (no VAT), change "inclusief btw" and the
      terms.
- [ ] **Terms** (`site/voorwaarden.html`) and **privacy statement**
      (`site/privacy.html`) are drafts marked "Concept". They need legal
      review and the retention period, then remove the banner.
- [ ] Processor agreements / sub-processors: Meta (WhatsApp), Mollie, Google
      (reverse geocoding). Add to the privacy statement and the processing
      register.
- [ ] Consumer law: the 14-day withdrawal right coincides with the trial
      (stated in the terms). Since 2023, Dutch law requires cancelling to be
      as easy as signing up. `opzeggen` on WhatsApp plus email probably
      covers it; a cancel button on the site is safer.
- [ ] Invoices: Mollie does not send VAT invoices for subscriptions. Business
      customers will ask for one (Mollie Invoicing, Moneybird or similar).

Mollie:
- [ ] Complete Mollie onboarding: KvK extract, bank account, UBO, ID, website
      review of rrbot.nl. Mollie checks that terms, privacy, prices and
      contact details are on the site.
- [ ] Mollie state on 2026-11-01: company details, stakeholders, business
      activity and bank type are done; "Open a Mollie Business Account" is
      open, and payouts are not yet available.
- [ ] Payment methods (profile `pfl_D9ZeJ6TNr5` › Online payments), checked
      2026-11-01: **iDEAL activated**; **Cards** and **SEPA Direct Debit** not
      yet. Until one of them is active, `GET /v2/methods?sequenceType=first`
      returns no methods at all, and a signup ends on the site's "Er ging
      iets mis" page. An iDEAL first payment needs SEPA Direct Debit for the
      mandate; a card mandates itself.
- [ ] The profile's other websites are pauta.cc and rit.du7.dev (both
      approved). **rrbot.nl is not listed**: add it, or replace rit.du7.dev,
      which is no longer used.
- [ ] Mollie's fee on each signup: the €0.01 mandate payment costs €0.32 via
      iDEAL. That's fine at this price, but it is why the mandate amount stays
      at the minimum.
- [ ] Enable **iDEAL** on the profile. In the test run, checkout offered
      only credit card, even though the site promises "iDEAL of creditcard".
      Also enable SEPA Direct Debit, which is how the recurring charges after
      an iDEAL first payment are collected.
- [ ] Create a separate Mollie **payment profile for rrbot.nl**. The current
      profile is "Du7 Technology Services / du7.dev", and Mollie reviews the
      profile's website before live payments. Live keys are tied to a
      profile.
- [ ] Replace the test key with the **live** key in 1Password, re-render the
      `.env`, restart.

Operations:
- [ ] `RIT_MAX_USERS` is 5, including the owner's car, so 4 subscribers. Raise
      it when ready.
- [ ] Admin notifications are WhatsApp texts. They only arrive within 24 hours
      of the admin's last message to the bot (error 131047). Either message
      the bot daily or add a utility template for admin alerts.
- [ ] Rate-limit `POST /aanmelden`. Each post creates a Mollie customer;
      Caddy's `rate_limit` needs a plugin build, so use fail2ban or an in-app
      limit per IP.
- [ ] Back up `data/` (ledgers, `signups.json`, per-car learned places) off
      the Pi.
- [ ] The bot's own replies are still in English; only the welcome/help and
      subscription messages are Dutch.
- [ ] The number is the Meta-provided +1 555; a Dutch number would look more
      trustworthy to customers.
- [ ] Watch the WhatsApp messaging limit tier as customers grow.
