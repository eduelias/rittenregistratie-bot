"""Paid subscriptions through Mollie: signup -> mandate -> subscription -> car.

Flow
----
1. The website form posts to ``/aanmelden``. :meth:`Billing.start` validates
   it, creates a Mollie customer and a small *first* payment (iDEAL or card).
   That payment is only there to give Mollie a mandate for the later charges.
   The customer is sent to Mollie's checkout.
2. Mollie calls ``/billing/mollie/webhook`` with the payment id, and the
   customer comes back on ``/aanmelden/klaar``. Both run :meth:`sync_payment`,
   which always fetches the payment from Mollie (a webhook carries nothing but
   an id, so it cannot be forged into a paid status) and is idempotent.
3. Once the first payment is paid: a subscription starts after the trial,
   the number gets a car in ``cars.yaml`` (``subscription: <signup id>``, no
   event plugins, the subscriber cap plugin) and the admins are told.
4. Over WhatsApp the subscriber manages it all (:meth:`Billing.handle`):
   ``abonnement`` shows the status and a numbered menu; ``opzeggen``,
   ``pauzeren``, ``hervatten``/``opnieuw``, ``maandelijks``/``jaarlijks`` act
   directly. See "Subscriber lifecycle" below.

Subscriber lifecycle
--------------------
Mollie has no pause and no plan change, so both are a cancel plus a new
subscription on the customer's existing mandate:

- *Cancel* (ACTIVE -> CANCELED): the number keeps working until the day
  before the next charge (or the trial end), then is removed on its next
  message (-> ENDED). The ledger stays on disk.
- *Pause* (ACTIVE -> PAUSED): the subscription is cancelled, the mandate and
  the car are kept. Trips are accepted until the already-paid period ends
  (``paid_until``); after that new trips are refused, but ``excel``,
  ``help`` and the subscription commands keep working. The car stays in
  ``cars.yaml`` (and counts towards RIT_MAX_USERS) so the ledger simply
  continues on resume.
- *Switch plan* (ACTIVE): cancel and recreate with the other interval and
  price, starting on the old next charge date; the current period runs out.
- *Resume / restart* (PAUSED, CANCELED, ENDED -> ACTIVE): a new subscription
  on a valid mandate, starting the day after the paid period (or today when
  that is over). ENDED numbers get their car back under the same car id, so
  the ledger continues. Without a usable mandate the customer gets a new
  first-payment link on the same customer; once paid, the same restart runs.
  There is no second trial.

Every change is reported to the admins.

A failed recurring payment is reported to the admins, who decide; the
number is not cut off automatically.

Signups are kept in ``data/signups.json``. Mollie is the source of truth for
money; this file only links a payment to a phone and a car.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from .cars import add_car_to_yaml, normalize_phone, remove_car_from_yaml, slugify_car_id
from .config import Settings
from .i18n import Key, date_text, money, render_parts, set_current, t

log = logging.getLogger("rittenregistratie.billing")

PLANS = ("monthly", "yearly")
_INTERVAL = {"monthly": "1 month", "yearly": "12 months"}
_PLAN_NL = {"monthly": "maandelijks", "yearly": "jaarlijks"}
_LOCALE = {"nl": "nl_NL", "en": "en_US"}   # Mollie checkout language per site language

# Signup.status values
PENDING = "pending"          # form sent, first payment not (yet) paid
ACTIVE = "active"            # paid, subscription created, car provisioned
FAILED = "failed"            # first payment failed / expired / canceled
CANCELED = "canceled"        # customer cancelled; works until access_until
PAUSED = "paused"            # subscription stopped, mandate + car kept; see paid_until
ENDED = "ended"              # access_until passed, number removed
ATTENTION = "attention"      # paid but could not be provisioned; admin acts

_PAYMENT_DEAD = ("failed", "canceled", "expired")


class SignupError(ValueError):
    """A form value the customer must correct. ``str()`` is the Dutch text,
    :meth:`text` the one for the form's language."""

    def __init__(self, nl: str, en: str = ""):
        super().__init__(nl)
        self.en = en or nl

    def text(self, lang: str) -> str:
        return self.en if lang == "en" else str(self)


class MollieError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class Signup:
    id: str
    name: str
    email: str
    phone: str
    car_label: str
    seed_odometer: int
    seed_address: str
    plan: str
    created_at: str
    status: str = PENDING
    customer_id: str = ""
    first_payment_id: str = ""
    subscription_id: str = ""
    car_id: str = ""
    access_until: str = ""      # ISO date, set when cancelled
    events: List[str] = field(default_factory=list)  # short audit trail
    mandate_id: str = ""        # the mandate the subscription charges
    trial_until: str = ""       # ISO date, last day of the free trial
    paid_until: str = ""        # ISO date, PAUSED: last day already paid for
    old_subscription_ids: List[str] = field(default_factory=list)
    # A first payment to renew the mandate before a resume/restart.
    renew_payment_id: str = ""
    renew_plan: str = ""
    renew_start: str = ""       # ISO date the new subscription should start
    data_deleted_at: str = ""   # set by retention.py once the trip data is gone
    # Optional, for invoices (invoices.py); the customer can change them later.
    company: str = ""
    billing_address: str = ""
    # The website language the customer signed up in (nl | en): the bot
    # speaks it to them from the start, and Mollie's checkout uses it.
    lang: str = "nl"

    def note(self, text: str) -> None:
        self.events.append(f"{datetime.now().isoformat(timespec='seconds')} {text}")

    def noted(self, text: str) -> bool:
        return any(e.endswith(f" {text}") for e in self.events)

    @property
    def renew_failed(self) -> bool:
        return any(self.noted(f"renew {self.renew_payment_id} {s}") for s in _PAYMENT_DEAD)


class SignupStore:
    """``data/signups.json``: {signup_id: Signup}."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8")) or {}

    def get(self, signup_id: str) -> Optional[Signup]:
        raw = self._load().get(signup_id or "")
        return Signup(**raw) if raw else None

    def save(self, signup: Signup) -> None:
        data = self._load()
        data[signup.id] = asdict(signup)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> List[Signup]:
        return [Signup(**v) for v in self._load().values()]

    def find(self, *, payment_id: str = "", subscription_id: str = "") -> Optional[Signup]:
        for s in self.all():
            if payment_id and payment_id in (s.first_payment_id, s.renew_payment_id):
                return s
            if subscription_id and (s.subscription_id == subscription_id
                                    or subscription_id in s.old_subscription_ids):
                return s
        return None

    def for_phone(self, phone: str) -> List[Signup]:
        """This number's signups, oldest first."""
        phone = normalize_phone(phone)
        return sorted((s for s in self.all() if s.phone == phone), key=lambda s: s.created_at)


class Mollie:
    """The few Mollie v2 calls this needs, over httpx."""

    def __init__(self, api_key: str, base_url: str = "https://api.mollie.com/v2",
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._transport = transport

    @property
    def test_mode(self) -> bool:
        return self._key.startswith("test_")

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            resp = await client.request(
                method, f"{self._base}{path}", json=body,
                headers={"Authorization": f"Bearer {self._key}"},
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise MollieError(f"Mollie {method} {path}: {resp.status_code} {detail}",
                              resp.status_code)
        return resp.json() if resp.content else {}

    async def create_customer(self, name: str, email: str, metadata: dict) -> dict:
        return await self._call("POST", "/customers",
                                {"name": name, "email": email, "metadata": metadata})

    async def create_payment(self, body: dict) -> dict:
        return await self._call("POST", "/payments", body)

    async def get_payment(self, payment_id: str) -> dict:
        return await self._call("GET", f"/payments/{payment_id}")

    async def create_subscription(self, customer_id: str, body: dict) -> dict:
        return await self._call("POST", f"/customers/{customer_id}/subscriptions", body)

    async def get_subscription(self, customer_id: str, subscription_id: str) -> dict:
        return await self._call("GET", f"/customers/{customer_id}/subscriptions/{subscription_id}")

    async def cancel_subscription(self, customer_id: str, subscription_id: str) -> dict:
        return await self._call("DELETE", f"/customers/{customer_id}/subscriptions/{subscription_id}")

    async def list_mandates(self, customer_id: str) -> List[dict]:
        data = await self._call("GET", f"/customers/{customer_id}/mandates")
        return (data.get("_embedded") or {}).get("mandates") or []


def amount(value: str) -> dict:
    return {"currency": "EUR", "value": f"{float(value):.2f}"}


def parse_phone(raw: str) -> str:
    """A WhatsApp number as typed in a Dutch form -> E.164 digits (no '+').

    ``06 12345678`` -> ``31612345678``; ``+31 6 ...`` and ``0031 6 ...``
    likewise; other countries must be typed with their country code.
    """
    digits = normalize_phone(raw)
    if digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 10 and digits.startswith("06"):
        digits = "31" + digits[1:]
    if not (10 <= len(digits) <= 15) or digits.startswith("0"):
        raise SignupError("Vul je WhatsApp-nummer in, bijvoorbeeld 06 12345678 of +31 6 12345678.",
                          "Enter your WhatsApp number, e.g. 06 12345678 or +31 6 12345678.")
    return digits


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


Notify = Callable[[str, str], Awaitable[object]]


class Billing:
    """Signup and subscription lifecycle. ``engine`` provides cars and reload;
    ``notify(phone, text)`` sends a WhatsApp message (admins, customers)."""

    def __init__(self, settings: Settings, engine, mollie: Mollie, notify: Notify,
                 alert: Optional[Notify] = None,
                 on_paid: Optional[Callable[[Signup, dict], Awaitable[object]]] = None):
        self.settings = settings
        self.engine = engine
        self.mollie = mollie
        self.notify = notify
        # Messages to admins: may need a template when their window is closed.
        # Called as alert(phone, text, event=<label>, subject=<subscriber number>).
        self.alert = alert or (lambda phone, text, **_: notify(phone, text))
        # Called (under the lock) for every paid subscription payment: invoices.
        self.on_paid = on_paid
        self.store = SignupStore(settings.signups_file)
        self._lock = asyncio.Lock()
        # phone -> (expiry on the monotonic clock, the menu's (action, plan)s).
        # In memory: after a restart a number just asks for the menu again.
        self._menus: Dict[str, Tuple[float, List[Tuple[str, Optional[str]]]]] = {}

    @staticmethod
    def _mandate_description(signup: Signup, plan: str, renew: bool = False) -> str:
        """What the customer sees at Mollie's checkout and on the bank statement."""
        if signup.lang == "en":
            return f"RittenRegistratie-bot mandate ({plan} plan{', restart' if renew else ''})"
        return (f"RittenRegistratie-bot machtiging ({_PLAN_NL[plan]} abonnement"
                f"{', hervatten' if renew else ''})")

    # --- urls ---------------------------------------------------------
    @property
    def _base(self) -> str:
        return self.settings.public_base_url.rstrip("/")

    def _webhook_url(self) -> Optional[str]:
        """None when the base URL is local: Mollie refuses webhooks it cannot
        reach, and the return page syncs the payment anyway."""
        base = self._base
        if re.match(r"^https?://(localhost|127\.|\[::1\])", base):
            return None
        return f"{base}/billing/mollie/webhook"

    def return_url(self, signup: Signup) -> str:
        return f"{self._base}/aanmelden/klaar?id={signup.id}"

    def price(self, plan: str) -> str:
        return self.settings.price_yearly if plan == "yearly" else self.settings.price_monthly

    # --- capacity -----------------------------------------------------
    def has_room(self) -> bool:
        return len(self.engine.cars.all()) < self.settings.max_users

    # --- 1. signup ----------------------------------------------------
    def validate(self, form: Dict[str, str]) -> Signup:
        name = (form.get("name") or "").strip()
        email = (form.get("email") or "").strip()
        label = (form.get("car_label") or "").strip()
        address = (form.get("seed_address") or "").strip()
        plan = (form.get("plan") or "monthly").strip()
        lang = "en" if form.get("lang") == "en" else "nl"
        company = " ".join((form.get("company") or "").split())
        billing_address = " ".join((form.get("billing_address") or "").split())
        if not name:
            raise SignupError("Vul je naam in.", "Enter your name.")
        if not _EMAIL_RE.match(email):
            raise SignupError("Vul een geldig e-mailadres in.", "Enter a valid email address.")
        phone = parse_phone(form.get("phone") or "")
        if not label:
            raise SignupError("Geef je auto een naam, bijvoorbeeld het kenteken.",
                              "Give your car a name, for example its number plate.")
        odo_raw = re.sub(r"[.\s]", "", form.get("seed_odometer") or "")
        if not odo_raw.isdigit() or not (0 <= int(odo_raw) <= 2_000_000):
            raise SignupError("Vul de huidige kilometerstand in (alleen cijfers).",
                              "Enter the current odometer reading (digits only).")
        if not address:
            raise SignupError("Vul het adres in waar je eerste rit begint.",
                              "Enter the address where your first trip starts.")
        if plan not in PLANS:
            raise SignupError("Kies maandelijks of jaarlijks.", "Choose monthly or yearly.")
        if form.get("terms") not in ("on", "yes", "true", "1"):
            raise SignupError("Ga akkoord met de voorwaarden om verder te gaan.",
                              "Accept the terms to continue.")
        if self.engine.cars.is_registered(phone):
            raise SignupError("Dit nummer gebruikt de bot al. Stuur 'abonnement' in WhatsApp.",
                              "This number already uses the bot. Send 'subscription' in WhatsApp.")
        return Signup(
            id=secrets.token_urlsafe(16), name=name[:100], email=email[:200], phone=phone,
            car_label=label[:60], seed_odometer=int(odo_raw), seed_address=address[:200],
            plan=plan, created_at=datetime.now().isoformat(timespec="seconds"),
            company=company[:100], billing_address=billing_address[:200], lang=lang,
        )

    async def start(self, form: Dict[str, str]) -> str:
        """Validate the form, create the Mollie customer and first payment;
        return the checkout URL. Raises SignupError for the customer."""
        signup = self.validate(form)
        if not self.has_room():
            raise SignupError("We zitten op dit moment vol. Probeer het later opnieuw.",
                              "We're full at the moment. Please try again later.")
        customer = await self.mollie.create_customer(
            signup.name, signup.email, {"signup_id": signup.id})
        signup.customer_id = customer["id"]
        body = {
            "amount": amount(self.settings.mandate_amount),
            "customerId": signup.customer_id,
            "sequenceType": "first",
            "description": self._mandate_description(signup, signup.plan),
            "redirectUrl": self.return_url(signup),
            "locale": _LOCALE[signup.lang],
            "metadata": {"signup_id": signup.id},
        }
        hook = self._webhook_url()
        if hook:
            body["webhookUrl"] = hook
        payment = await self.mollie.create_payment(body)
        signup.first_payment_id = payment["id"]
        signup.note(f"first payment {payment['id']} created")
        self.store.save(signup)
        log.info("Signup %s started (%s, plan %s).", signup.id, payment["id"], signup.plan)
        return payment["_links"]["checkout"]["href"]

    # --- 2. payments --------------------------------------------------
    async def sync_payment(self, payment_id: str) -> Optional[Signup]:
        """Bring our state in line with one Mollie payment. Safe to repeat."""
        if not payment_id or not payment_id.startswith("tr_"):
            return None
        async with self._lock:
            try:
                payment = await self.mollie.get_payment(payment_id)
            except MollieError as exc:
                if exc.status == 404:  # not ours (or other mode's key): nothing to do
                    log.warning("Mollie does not know payment %s.", payment_id)
                    return None
                raise
            if payment.get("subscriptionId"):
                return await self._recurring(payment)
            signup = self.store.find(payment_id=payment_id)
            if signup is None:
                log.warning("Mollie payment %s belongs to no signup.", payment_id)
                return None
            if payment_id == signup.renew_payment_id:
                await self._renewal(signup, payment)
                return signup
            status = payment.get("status")
            if signup.status == PENDING and status == "paid":
                await self._activate(signup, payment)
            elif signup.status == PENDING and status in _PAYMENT_DEAD:
                signup.status = FAILED
                signup.note(f"first payment {status}")
                self.store.save(signup)
            return signup

    async def sync_signup(self, signup_id: str) -> Optional[Signup]:
        """Used by the return page: sync the signup's first payment."""
        signup = self.store.get(signup_id)
        if signup is None:
            return None
        if signup.status == PENDING and signup.first_payment_id:
            return await self.sync_payment(signup.first_payment_id) or signup
        if (signup.renew_payment_id and signup.status in (PAUSED, CANCELED, ENDED)
                and not signup.renew_failed):
            return await self.sync_payment(signup.renew_payment_id) or signup
        return signup

    async def _subscribe(self, signup: Signup, plan: str, start: date, mandate_id: str) -> dict:
        """Create a Mollie subscription for ``plan`` from ``start`` and make it
        the signup's current one (the previous id is kept for its webhooks).
        Does not save."""
        body = {
            "amount": amount(self.price(plan)),
            "interval": _INTERVAL[plan],
            "startDate": start.isoformat(),
            "description": f"RittenRegistratie-bot {_PLAN_NL[plan]} ({signup.car_label}, {signup.id[:6]})",
            "metadata": {"signup_id": signup.id},
        }
        if mandate_id:
            body["mandateId"] = mandate_id
            signup.mandate_id = mandate_id
        hook = self._webhook_url()
        if hook:
            body["webhookUrl"] = hook
        sub = await self.mollie.create_subscription(signup.customer_id, body)
        if signup.subscription_id and signup.subscription_id != sub["id"]:
            signup.old_subscription_ids.append(signup.subscription_id)
        signup.subscription_id = sub["id"]
        signup.plan = plan
        signup.note(f"subscription {sub['id']} ({plan}) from {start.isoformat()}")
        return sub

    async def _activate(self, signup: Signup, payment: dict) -> None:
        if signup.lang == "en" and self.engine.languages.get(signup.phone) is None:
            # Signed up on the English site: the bot speaks English from the start.
            self.engine.languages.set(signup.phone, "en")
        start = date.today() + timedelta(days=self.settings.trial_days)
        sub = await self._subscribe(signup, signup.plan, start, payment.get("mandateId") or "")
        signup.trial_until = (start - timedelta(days=1)).isoformat()
        # The subscription exists from here on; save before provisioning so a
        # failure below cannot lead to a second subscription on the retry.
        signup.status = ATTENTION
        self.store.save(signup)
        try:
            signup.car_id = self._provision(signup)
        except ValueError as exc:
            signup.note(f"provisioning failed: {exc}")
            self.store.save(signup)
            await self._tell_admins("alert.provision_failed", id=signup.id, phone=signup.phone,
                                    car=signup.car_label, error=exc, sub=sub["id"])
            return
        signup.status = ACTIVE
        signup.note(f"car {signup.car_id} provisioned")
        self.store.save(signup)
        await self._tell_admins("alert.new", name=signup.name, phone=signup.phone,
                                car=signup.car_label, plan=Key(f"plan.{signup.plan}"),
                                trial=start.isoformat(), car_id=signup.car_id)

    def _provision(self, signup: Signup) -> str:
        """Add the subscriber's car to cars.yaml. Over RIT_MAX_USERS is allowed
        here: the customer has paid; the website refuses new signups instead."""
        if self.engine.cars.is_registered(signup.phone):
            raise ValueError(f"{signup.phone} is already registered")
        car_id = slugify_car_id(signup.car_label, self._taken_car_ids())
        add_car_to_yaml(
            self.settings.cars_file, car_id, signup.car_label, signup.seed_address,
            signup.seed_odometer, signup.phone,
            event_plugins=[], cap_plugin=self.settings.subscriber_cap_plugin,
            subscription=signup.id,
        )
        self.engine.reload_cars()
        return car_id

    def _taken_car_ids(self) -> List[str]:
        """Car ids a new subscriber may not get: cars in cars.yaml, ids held by
        any signup (an ENDED subscriber gets theirs back on restart) and any id
        with a ledger on disk. Reusing one would hand someone else's trips to
        the new number."""
        taken = {c.car_id for c in self.engine.cars.all()}
        taken.update(s.car_id for s in self.store.all() if s.car_id)
        taken.update(p.name[len("raw-ledger-"):-len(".jsonl")]
                     for p in self.settings.data_dir.glob("raw-ledger-*.jsonl"))
        return sorted(taken)

    async def _recurring(self, payment: dict) -> Optional[Signup]:
        signup = self.store.find(subscription_id=payment["subscriptionId"])
        if signup is None:
            log.warning("Recurring payment %s for unknown subscription %s.",
                        payment.get("id"), payment.get("subscriptionId"))
            return None
        status = payment.get("status")
        marker = f"recurring {payment.get('id')} {status}"
        if marker in " ".join(signup.events):
            return signup
        signup.note(marker)
        self.store.save(signup)
        if status in _PAYMENT_DEAD or payment.get("amountChargedBack"):
            await self._tell_admins("alert.charge_failed", payment=payment.get("id"),
                                    name=signup.name, phone=signup.phone, car=signup.car_id,
                                    status=status)
        elif status == "paid" and self.on_paid is not None:
            try:
                await self.on_paid(signup, payment)
            except Exception:  # an invoice problem must not break billing
                log.exception("on_paid failed for payment %s", payment.get("id"))
                await self._tell_admins("alert.invoice_failed", payment=payment.get("id"),
                                        name=signup.name, phone=signup.phone)
        return signup

    async def set_billing_details(self, phone: str, *, company: Optional[str] = None,
                                  address: Optional[str] = None) -> Optional[Signup]:
        """Change the invoice details of this number's current signup ('-'
        clears one). None when the number has no signup."""
        async with self._lock:
            signup = self._current(phone)
            if signup is None:
                return None
            if company is not None:
                signup.company = "" if company == "-" else " ".join(company.split())[:100]
            if address is not None:
                signup.billing_address = "" if address == "-" else " ".join(address.split())[:200]
            signup.note("billing details changed")
            self.store.save(signup)
            return signup

    def signup_for(self, phone: str) -> Optional[Signup]:
        """This number's current signup (see :meth:`_current`), or None."""
        return self._current(phone)

    # --- 3. subscriber commands over WhatsApp -------------------------
    def _current(self, phone: str) -> Optional[Signup]:
        """The signup this number's commands act on: its latest live one,
        else its latest ended one (so a former subscriber can restart)."""
        mine = self.store.for_phone(phone)
        live = [s for s in mine if s.status in (ACTIVE, PAUSED, CANCELED, ATTENTION)]
        ended = [s for s in mine if s.status == ENDED]
        return (live or ended or [None])[-1]

    def can_cancel(self, phone: str) -> bool:
        """True when this number has a subscription the website may cancel."""
        signup = self._current(phone)
        return signup is not None and signup.status in (ACTIVE, PAUSED)

    def access_until(self, phone: str) -> str:
        """The last day a cancelled number may use the bot (ISO), or ''."""
        signup = self._current(phone)
        return signup.access_until if signup is not None and signup.status == CANCELED else ""

    def returning(self, phone: str) -> bool:
        """True for a number whose subscription ended (its car was removed):
        it may still use the subscription commands to come back."""
        signup = self._current(phone)
        return signup is not None and signup.status == ENDED

    async def handle(self, phone: str, text: str, today: Optional[date] = None) -> Optional[str]:
        """The reply to a subscription command or a menu pick; None when
        ``text`` is neither (the caller handles it as a trip or command)."""
        today = today or date.today()
        set_current(self._lang(phone))
        word = (text or "").strip().lower().lstrip("/")
        menu = self._menus.get(normalize_phone(phone))
        if word.isdigit() and menu is not None and menu[0] > time.monotonic():
            actions = menu[1]
            if not 1 <= int(word) <= len(actions):
                return t("menu.range", n=len(actions))
            self._menus.pop(normalize_phone(phone), None)
            action, plan = actions[int(word) - 1]
        elif word in COMMANDS:
            action, plan = COMMANDS[word], None
            if action in PLANS:
                action, plan = "plan", action
        else:
            return None
        signup = self._current(phone)
        if signup is None:
            return self._no_subscription()
        if action == "menu":
            return await self._menu(signup, today)
        async with self._lock:
            signup = self._current(phone)  # may have changed while we waited
            return await self._act(signup, action, plan, today)

    async def _act(self, signup: Signup, action: str, plan: Optional[str], today: date) -> str:
        state = signup.status
        if state == ATTENTION:
            return self._attention()
        if state == ACTIVE:
            if action == "cancel":
                return await self._cancel_active(signup, today)
            if action == "pause":
                return await self._pause(signup, today)
            if action == "plan" and plan and plan != signup.plan:
                return await self._switch(signup, plan, today)
            if action == "plan":
                return t("sub.already_plan", plan=self._plan(signup.plan))
            return t("sub.running")
        if action in ("resume", "plan"):
            return await self._continue(signup, plan or signup.plan, today)
        if state == PAUSED and action == "cancel":
            return await self._cancel_paused(signup, today)
        if state == PAUSED:
            return t("sub.already_paused")
        if state == CANCELED:
            return t("sub.already_cancelled", date=date_text(signup.access_until))
        return t("sub.already_ended")

    # status + menu
    async def _menu(self, signup: Signup, today: date) -> str:
        if signup.status == ATTENTION:
            return self._attention()
        plan, other = signup.plan, _other(signup.plan)
        lines: List[str] = []
        if signup.status == ACTIVE:
            sub = await self.mollie.get_subscription(signup.customer_id, signup.subscription_id)
            lines.append(t("menu.active", plan=self._plan(plan), price=self._price_text(plan)))
            if signup.trial_until and date.fromisoformat(signup.trial_until) >= today:
                lines.append(t("menu.trial", date=date_text(signup.trial_until)))
            if sub.get("status") not in (None, "active"):
                lines.append(t("menu.mollie", status=sub.get("status")))
            nxt = sub.get("nextPaymentDate")
            if nxt:
                lines.append(t("menu.next", date=date_text(nxt), amount=self._amount_text(plan)))
            lines.append(t("menu.method", method=_mandate_text(await self._usable_mandate(signup))))
            options = [("plan", other), ("pause", None), ("cancel", None)]
        elif signup.status == PAUSED:
            lines.append(t("menu.paused", plan=self._plan(plan)))
            if self._paid(signup.paid_until, today):
                lines.append(t("menu.paused_usable", date=date_text(signup.paid_until)))
            else:
                lines.append(t("menu.paused_closed"))
            lines.append(t("menu.method", method=_mandate_text(await self._usable_mandate(signup))))
            options = [("resume", plan), ("resume", other), ("cancel", None)]
        elif signup.status == CANCELED:
            lines.append(t("menu.cancelled"))
            if self._paid(signup.access_until, today):
                lines.append(t("menu.works_until", date=date_text(signup.access_until)))
            options = [("resume", plan), ("resume", other)]
        else:  # ENDED
            lines.append(t("menu.ended"))
            options = [("resume", plan), ("resume", other)]
        lines += ["", t("menu.choose")]
        for i, (action, p) in enumerate(options, 1):
            lines.append(f"{i}. {self._option_text(signup, action, p)}")
        self._menus[signup.phone] = (time.monotonic() + MENU_TTL_S, options)
        return "\n".join(lines)

    def _option_text(self, signup: Signup, action: str, plan: Optional[str]) -> str:
        if action == "plan":
            return t("opt.switch", plan=self._plan(plan), price=self._price_text(plan))
        if action == "pause":
            return t("opt.pause")
        if action == "cancel":
            return t("opt.cancel_final" if signup.status == PAUSED else "opt.cancel")
        key = "opt.resume" if signup.status == PAUSED else "opt.restart"
        return t(key, plan=self._plan(plan), price=self._price_text(plan))

    # cancel / pause / switch (ACTIVE)
    async def _next_charge(self, signup: Signup, today: date) -> date:
        """The date the current subscription charges next (or starts, in the
        trial). Mollie omits it once nothing more will be charged."""
        sub = await self.mollie.get_subscription(signup.customer_id, signup.subscription_id)
        nxt = sub.get("nextPaymentDate") or sub.get("startDate")
        return date.fromisoformat(nxt) if nxt else today

    async def _cancel_active(self, signup: Signup, today: date) -> str:
        nxt = await self._next_charge(signup, today)
        await self.mollie.cancel_subscription(signup.customer_id, signup.subscription_id)
        signup.status = CANCELED
        signup.access_until = max(nxt - timedelta(days=1), today).isoformat()
        signup.note(f"cancelled by customer, access until {signup.access_until}")
        self.store.save(signup)
        await self._tell_admins("alert.cancelled", name=signup.name, phone=signup.phone,
                                date=signup.access_until)
        return t("sub.cancelled", date=date_text(signup.access_until))

    async def _pause(self, signup: Signup, today: date) -> str:
        nxt = await self._next_charge(signup, today)
        if nxt <= today:
            return t("sub.charge_day")
        await self.mollie.cancel_subscription(signup.customer_id, signup.subscription_id)
        signup.status = PAUSED
        signup.paid_until = (nxt - timedelta(days=1)).isoformat()
        signup.note(f"paused by customer, paid until {signup.paid_until}")
        self.store.save(signup)
        await self._tell_admins("alert.paused", name=signup.name, phone=signup.phone,
                                car=signup.car_id, date=signup.paid_until)
        return t("sub.paused", date=date_text(signup.paid_until))

    async def _switch(self, signup: Signup, plan: str, today: date) -> str:
        nxt = await self._next_charge(signup, today)
        if nxt <= today:
            return t("sub.charge_day")
        mandate = await self._usable_mandate(signup)
        if mandate is None:
            return t("sub.switch_no_mandate", support=self._support())
        old = signup.subscription_id
        await self.mollie.cancel_subscription(signup.customer_id, old)
        try:
            await self._subscribe(signup, plan, nxt, mandate["id"])
        except MollieError:
            # The old subscription is gone and no new one exists: hold the
            # number as paused (paid period intact) and let the owner fix it.
            log.exception("plan switch for %s failed after the cancel", signup.id)
            signup.status = PAUSED
            signup.paid_until = (nxt - timedelta(days=1)).isoformat()
            signup.note(f"switch to {plan} failed after cancelling {old}; paused")
            self.store.save(signup)
            await self._tell_admins("alert.switch_failed", name=signup.name, phone=signup.phone,
                                    plan=Key(f"plan.{plan}"), old=old)
            return t("sub.switch_failed", support=self._support("via"))
        self.store.save(signup)
        await self._tell_admins("alert.switched", name=signup.name, phone=signup.phone,
                                plan=Key(f"plan.{plan}"), date=nxt.isoformat())
        return t("sub.switched", plan=self._plan(plan), date=date_text(nxt),
                 price=self._price_text(plan))

    # cancel while paused
    async def _cancel_paused(self, signup: Signup, today: date) -> str:
        signup.status = CANCELED
        signup.access_until = signup.paid_until or today.isoformat()
        signup.note(f"cancelled while paused, access until {signup.access_until}")
        self.store.save(signup)
        await self._tell_admins("alert.cancelled_paused", name=signup.name, phone=signup.phone,
                                date=signup.access_until)
        if self._paid(signup.access_until, today):
            return t("sub.cancelled_paused", date=date_text(signup.access_until))
        return t("sub.cancelled_paused_now")

    # resume / restart (PAUSED, CANCELED, ENDED)
    async def _continue(self, signup: Signup, plan: str, today: date) -> str:
        if signup.status == PAUSED:
            start = _day_after(signup.paid_until, today)
        elif signup.status == CANCELED:
            start = _day_after(signup.access_until, today)
        else:
            start = today
        refusal = self._car_refusal(signup)
        if refusal:
            return refusal
        mandate = await self._usable_mandate(signup)
        if mandate is None:
            url = await self._renew_link(signup, plan, start)
            await self._tell_admins("alert.no_mandate", name=signup.name, phone=signup.phone,
                                    plan=Key(f"plan.{plan}"))
            return t("sub.renew_link", amount=money(self.settings.mandate_amount),
                     plan=self._plan(plan), price=self._price_text(plan), url=url)
        was = signup.status
        await self._subscribe(signup, plan, start, mandate["id"])
        self.store.save(signup)
        problem = await self._reinstate(signup)
        if problem:
            return problem
        await self._tell_admins("alert.continues", name=signup.name, phone=signup.phone,
                                car=signup.car_id, state=Key(f"state.{was}"),
                                plan=Key(f"plan.{plan}"), date=start.isoformat())
        when = t("when.today") if start <= today else t("when.on", date=date_text(start))
        return t("sub.running_again", plan=self._plan(plan), price=self._price_text(plan),
                 when=when, method=_mandate_text(mandate))

    def _car_refusal(self, signup: Signup) -> Optional[str]:
        """Why this signup cannot get its car back (checked before any money
        moves); None when the car is still there or can be re-added."""
        if self._has_car(signup):
            return None
        if self.engine.cars.is_registered(signup.phone):
            return t("sub.other_car", support=self._support())
        if signup.car_id and self.engine.cars.get(signup.car_id) is not None:
            return t("sub.car_blocked", support=self._support())
        if not self.has_room():
            return t("sub.full")
        return None

    def _has_car(self, signup: Signup) -> bool:
        return any(c.car_id == signup.car_id for c in self.engine.cars.cars_for(signup.phone))

    async def _reinstate(self, signup: Signup) -> Optional[str]:
        """After a new subscription: make sure the car is there (same car id,
        so the ledger continues) and mark the signup ACTIVE. Saves. Returns the
        customer's reply when the car could not be added."""
        if not self._has_car(signup):
            try:
                if signup.car_id:
                    add_car_to_yaml(
                        self.settings.cars_file, signup.car_id, signup.car_label,
                        signup.seed_address, signup.seed_odometer, signup.phone,
                        event_plugins=[], cap_plugin=self.settings.subscriber_cap_plugin,
                        subscription=signup.id,
                    )
                    self.engine.reload_cars()
                else:
                    signup.car_id = self._provision(signup)
            except ValueError as exc:
                signup.status = ATTENTION
                signup.note(f"re-provisioning failed: {exc}")
                self.store.save(signup)
                await self._tell_admins("alert.reinstate_failed", name=signup.name,
                                        phone=signup.phone, car=signup.car_id, error=exc,
                                        sub=signup.subscription_id)
                return t("sub.manual")
            signup.note(f"car {signup.car_id} back in cars.yaml")
        signup.status = ACTIVE
        signup.access_until = signup.paid_until = ""
        self.store.save(signup)
        return None

    async def _renew_link(self, signup: Signup, plan: str, start: date) -> str:
        """A first payment on the existing customer for a fresh mandate; the
        restart runs when it is paid (:meth:`_renewal`). Reuses an open one."""
        if signup.renew_payment_id and not signup.renew_failed:
            try:
                open_payment = await self.mollie.get_payment(signup.renew_payment_id)
            except MollieError:
                open_payment = {}
            href = ((open_payment.get("_links") or {}).get("checkout") or {}).get("href")
            if open_payment.get("status") == "open" and href:
                signup.renew_plan, signup.renew_start = plan, start.isoformat()
                self.store.save(signup)
                return href
        body = {
            "amount": amount(self.settings.mandate_amount),
            "customerId": signup.customer_id,
            "sequenceType": "first",
            "description": self._mandate_description(signup, plan, renew=True),
            "redirectUrl": self.return_url(signup),
            "locale": _LOCALE[signup.lang],
            "metadata": {"signup_id": signup.id, "renew": True},
        }
        hook = self._webhook_url()
        if hook:
            body["webhookUrl"] = hook
        payment = await self.mollie.create_payment(body)
        signup.renew_payment_id = payment["id"]
        signup.renew_plan, signup.renew_start = plan, start.isoformat()
        signup.note(f"renew payment {payment['id']} created ({plan})")
        self.store.save(signup)
        return payment["_links"]["checkout"]["href"]

    async def _renewal(self, signup: Signup, payment: dict) -> None:
        """A renew payment changed (webhook or return page). Caller holds the lock."""
        status = payment.get("status")
        marker = f"renew {payment['id']} {status}"
        if signup.noted(marker):
            return
        if status in _PAYMENT_DEAD:
            signup.note(marker)
            self.store.save(signup)
            return
        if status != "paid" or signup.status not in (PAUSED, CANCELED, ENDED):
            return
        set_current(self._lang(signup.phone))  # a Mollie webhook: write to the customer
        today = date.today()
        start = max(date.fromisoformat(signup.renew_start), today) if signup.renew_start else today
        plan = signup.renew_plan or signup.plan
        await self._subscribe(signup, plan, start, payment.get("mandateId") or "")
        signup.note(marker)
        self.store.save(signup)
        problem = await self._reinstate(signup)
        if problem is None:
            await self._tell_admins("alert.renewed", name=signup.name, phone=signup.phone,
                                    car=signup.car_id, plan=Key(f"plan.{plan}"),
                                    date=start.isoformat())
        try:
            await self.notify(signup.phone, problem or t(
                "sub.running_again_short", plan=self._plan(plan), price=self._price_text(plan)))
        except Exception:  # pragma: no cover - the restart itself succeeded
            log.exception("customer notification failed")

    async def _usable_mandate(self, signup: Signup) -> Optional[dict]:
        """A valid (or still pending) mandate of this customer, preferring the
        one on file; None when there is none."""
        if not signup.customer_id:
            return None
        ok = [m for m in await self.mollie.list_mandates(signup.customer_id)
              if m.get("status") in ("valid", "pending")]
        ok.sort(key=lambda m: m.get("id") != signup.mandate_id)
        return ok[0] if ok else None

    # --- kept for callers and tests ----------------------------------
    async def cancel(self, phone: str, today: Optional[date] = None) -> str:
        return await self.handle(phone, "opzeggen", today) or self._no_subscription()

    def expired(self, phone: str, today: Optional[date] = None) -> Optional[str]:
        """If this number's subscription has run out, remove its car and return
        the reply to send; None while it may still use the bot."""
        today = today or date.today()
        signup = self._current(phone)
        if signup is None or signup.status != CANCELED or not signup.access_until:
            return None
        if date.fromisoformat(signup.access_until) >= today:
            return None
        if signup.car_id:
            remove_car_from_yaml(self.settings.cars_file, signup.car_id)
            self.engine.reload_cars()
        signup.status = ENDED
        signup.note("access ended; car removed from cars.yaml (ledger kept)")
        self.store.save(signup)
        return self.ended_text(signup)

    def ended_text(self, signup=None) -> str:
        """The notice for a number whose subscription ended. Pass the phone's
        signup (or the phone itself) so it can say whether the trips are kept."""
        if isinstance(signup, str):
            signup = self._current(signup)
        if signup is not None and signup.data_deleted_at:
            return t("sub.ended_deleted")
        return t("sub.ended", days=self.settings.retention_days, support=self._support("via"))

    def paused(self, phone: str, today: Optional[date] = None) -> Optional[str]:
        """The reply refusing a new trip from a paused number whose paid period
        is over; None when trips are accepted."""
        today = today or date.today()
        signup = self._current(phone)
        if signup is None or signup.status != PAUSED or self._paid(signup.paid_until, today):
            return None
        return t("sub.paused_block")

    # --- texts --------------------------------------------------------
    @staticmethod
    def _paid(until: str, today: date) -> bool:
        return bool(until) and date.fromisoformat(until) >= today

    def _lang(self, phone: str) -> str:
        return self.engine.languages.of(phone)

    @staticmethod
    def _plan(plan: str) -> str:
        return t(f"plan.{plan}")

    def _price_text(self, plan: str) -> str:
        return t("price", amount=money(self.price(plan)), unit=t(f"unit.{plan}"))

    def _amount_text(self, plan: str) -> str:
        return f"€ {money(self.price(plan))}"

    def _support(self, how: str = "at") -> str:
        email = self.settings.support_email
        return t(f"support.{how}", email=email) if email else ""

    def _no_subscription(self) -> str:
        url = self.settings.signup_url
        return t("sub.none_signup", url=url) if url else t("sub.none")

    def _attention(self) -> str:
        return t("sub.attention")

    async def _tell_admins(self, key: str, **kw) -> None:
        """An alert to every admin, each in their own language."""
        for admin in self.settings.admin_list():
            try:
                lang = self._lang(admin)
                await self.alert(admin, render_parts([(key, kw)], lang),
                                 event=t(key.replace("alert.", "event."), lang),
                                 subject=str(kw.get("phone", "")))
            except Exception:  # pragma: no cover - a notification must not break billing
                log.exception("admin notification failed")


# WhatsApp words -> action ("menu", "cancel", "pause", "resume") or a plan.
COMMANDS = {
    "abonnement": "menu", "abo": "menu", "subscription": "menu",
    "opzeggen": "cancel", "unsubscribe": "cancel",
    "pauzeren": "pause", "pauze": "pause", "pause": "pause",
    "hervatten": "resume", "opnieuw": "resume", "verlengen": "resume", "resume": "resume",
    "restart": "resume", "renew": "resume",
    "maandelijks": "monthly", "jaarlijks": "yearly", "monthly": "monthly", "yearly": "yearly",
}
MENU_TTL_S = 30 * 60   # a numbered pick must follow the menu within this time



def _other(plan: str) -> str:
    return "yearly" if plan == "monthly" else "monthly"


def _day_after(until: str, today: date) -> date:
    """The first unpaid day after ``until``, but not before today."""
    return max(date.fromisoformat(until) + timedelta(days=1), today) if until else today


def _mandate_text(mandate: Optional[dict]) -> str:
    if not mandate:
        return t("mandate.none")
    details = mandate.get("details") or {}
    method = mandate.get("method")
    if method == "directdebit":
        tail = (details.get("consumerAccount") or "").replace(" ", "")[-4:]
        text = t("mandate.sepa_tail", tail=tail) if tail else t("mandate.sepa")
    elif method == "creditcard":
        tail = (details.get("cardNumber") or "")[-4:]
        label = details.get("cardLabel") or t("mandate.card")
        text = t("mandate.card_tail", label=label, tail=tail) if tail else label
    elif method == "paypal":
        text = "PayPal"
    else:
        text = method or t("mandate.unknown")
    if mandate.get("status") == "pending":
        text += t("mandate.pending")
    return text
