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
4. ``opzeggen`` over WhatsApp cancels the subscription; the number keeps
   working until the paid period (or the trial) ends, then is removed on its
   next message. The ledger stays on disk.

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
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import httpx

from .cars import add_car_to_yaml, normalize_phone, remove_car_from_yaml, slugify_car_id
from .config import Settings

log = logging.getLogger("rittenregistratie.billing")

PLANS = ("monthly", "yearly")
_INTERVAL = {"monthly": "1 month", "yearly": "12 months"}
_PLAN_NL = {"monthly": "maandelijks", "yearly": "jaarlijks"}

# Signup.status values
PENDING = "pending"          # form sent, first payment not (yet) paid
ACTIVE = "active"            # paid, subscription created, car provisioned
FAILED = "failed"            # first payment failed / expired / canceled
CANCELED = "canceled"        # customer cancelled; works until access_until
ENDED = "ended"              # access_until passed, number removed
ATTENTION = "attention"      # paid but could not be provisioned; admin acts

_PAYMENT_DEAD = ("failed", "canceled", "expired")


class SignupError(ValueError):
    """A form value the customer must correct (message is shown to them, Dutch)."""


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

    def note(self, text: str) -> None:
        self.events.append(f"{datetime.now().isoformat(timespec='seconds')} {text}")


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
            if payment_id and s.first_payment_id == payment_id:
                return s
            if subscription_id and s.subscription_id == subscription_id:
                return s
        return None

    def for_phone(self, phone: str) -> List[Signup]:
        phone = normalize_phone(phone)
        return [s for s in self.all() if s.phone == phone]


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
        raise SignupError("Vul je WhatsApp-nummer in, bijvoorbeeld 06 12345678 of +31 6 12345678.")
    return digits


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


Notify = Callable[[str, str], Awaitable[object]]


class Billing:
    """Signup and subscription lifecycle. ``engine`` provides cars and reload;
    ``notify(phone, text)`` sends a WhatsApp message (admins, customers)."""

    def __init__(self, settings: Settings, engine, mollie: Mollie, notify: Notify):
        self.settings = settings
        self.engine = engine
        self.mollie = mollie
        self.notify = notify
        self.store = SignupStore(settings.signups_file)
        self._lock = asyncio.Lock()

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
        if not name:
            raise SignupError("Vul je naam in.")
        if not _EMAIL_RE.match(email):
            raise SignupError("Vul een geldig e-mailadres in.")
        phone = parse_phone(form.get("phone") or "")
        if not label:
            raise SignupError("Geef je auto een naam, bijvoorbeeld het kenteken.")
        odo_raw = re.sub(r"[.\s]", "", form.get("seed_odometer") or "")
        if not odo_raw.isdigit() or not (0 <= int(odo_raw) <= 2_000_000):
            raise SignupError("Vul de huidige kilometerstand in (alleen cijfers).")
        if not address:
            raise SignupError("Vul het adres in waar je eerste rit begint.")
        if plan not in PLANS:
            raise SignupError("Kies maandelijks of jaarlijks.")
        if form.get("terms") not in ("on", "yes", "true", "1"):
            raise SignupError("Ga akkoord met de voorwaarden om verder te gaan.")
        if self.engine.cars.is_registered(phone):
            raise SignupError("Dit nummer gebruikt de bot al. Stuur 'abonnement' in WhatsApp.")
        return Signup(
            id=secrets.token_urlsafe(16), name=name[:100], email=email[:200], phone=phone,
            car_label=label[:60], seed_odometer=int(odo_raw), seed_address=address[:200],
            plan=plan, created_at=datetime.now().isoformat(timespec="seconds"),
        )

    async def start(self, form: Dict[str, str]) -> str:
        """Validate the form, create the Mollie customer and first payment;
        return the checkout URL. Raises SignupError for the customer."""
        signup = self.validate(form)
        if not self.has_room():
            raise SignupError("We zitten op dit moment vol. Probeer het later opnieuw.")
        customer = await self.mollie.create_customer(
            signup.name, signup.email, {"signup_id": signup.id})
        signup.customer_id = customer["id"]
        body = {
            "amount": amount(self.settings.mandate_amount),
            "customerId": signup.customer_id,
            "sequenceType": "first",
            "description": f"RRBot machtiging ({_PLAN_NL[signup.plan]} abonnement)",
            "redirectUrl": self.return_url(signup),
            "locale": "nl_NL",
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
        return signup

    async def _activate(self, signup: Signup, payment: dict) -> None:
        start = date.today() + timedelta(days=self.settings.trial_days)
        body = {
            "amount": amount(self.price(signup.plan)),
            "interval": _INTERVAL[signup.plan],
            "startDate": start.isoformat(),
            "description": f"RRBot {_PLAN_NL[signup.plan]} ({signup.car_label}, {signup.id[:6]})",
            "metadata": {"signup_id": signup.id},
        }
        if payment.get("mandateId"):
            body["mandateId"] = payment["mandateId"]
        hook = self._webhook_url()
        if hook:
            body["webhookUrl"] = hook
        sub = await self.mollie.create_subscription(signup.customer_id, body)
        signup.subscription_id = sub["id"]
        signup.note(f"subscription {sub['id']} from {start.isoformat()}")
        # The subscription exists from here on; save before provisioning so a
        # failure below cannot lead to a second subscription on the retry.
        signup.status = ATTENTION
        self.store.save(signup)
        try:
            signup.car_id = self._provision(signup)
        except ValueError as exc:
            signup.note(f"provisioning failed: {exc}")
            self.store.save(signup)
            await self._tell_admins(
                f"Betaalde aanmelding {signup.id} ({signup.phone}, {signup.car_label}) kon "
                f"niet worden ingericht: {exc}. Abonnement {sub['id']} loopt."
            )
            return
        signup.status = ACTIVE
        signup.note(f"car {signup.car_id} provisioned")
        self.store.save(signup)
        await self._tell_admins(
            f"Nieuwe abonnee: {signup.name} ({signup.phone}), {signup.car_label}, "
            f"{_PLAN_NL[signup.plan]}, proef tot {start.isoformat()}. Auto: {signup.car_id}."
        )

    def _provision(self, signup: Signup) -> str:
        """Add the subscriber's car to cars.yaml. Over RIT_MAX_USERS is allowed
        here: the customer has paid; the website refuses new signups instead."""
        if self.engine.cars.is_registered(signup.phone):
            raise ValueError(f"{signup.phone} is already registered")
        existing = [c.car_id for c in self.engine.cars.all()]
        car_id = slugify_car_id(signup.car_label, existing)
        add_car_to_yaml(
            self.settings.cars_file, car_id, signup.car_label, signup.seed_address,
            signup.seed_odometer, signup.phone,
            event_plugins=[], cap_plugin=self.settings.subscriber_cap_plugin,
            subscription=signup.id,
        )
        self.engine.reload_cars()
        return car_id

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
            await self._tell_admins(
                f"Incasso {payment.get('id')} van {signup.name} ({signup.phone}, "
                f"{signup.car_id}) is {status}. Nummer blijft actief; beslis zelf "
                f"(verwijderen: 'remove {signup.phone}')."
            )
        return signup

    # --- 3. subscriber commands over WhatsApp -------------------------
    def _current(self, phone: str) -> Optional[Signup]:
        live = [s for s in self.store.for_phone(phone) if s.status in (ACTIVE, CANCELED)]
        return live[-1] if live else None

    async def status_text(self, phone: str) -> str:
        signup = self._current(phone)
        if signup is None:
            return "Ik vind geen abonnement bij dit nummer."
        plan = f"{_PLAN_NL[signup.plan]}, € {self.price(signup.plan).replace('.', ',')}"
        if signup.status == CANCELED:
            return f"Abonnement ({plan}) is opgezegd; de bot werkt tot en met {signup.access_until}."
        sub = await self.mollie.get_subscription(signup.customer_id, signup.subscription_id)
        nxt = sub.get("nextPaymentDate") or "onbekend"
        return f"Abonnement actief ({plan}). Volgende betaling: {nxt}. Opzeggen: stuur 'opzeggen'."

    async def cancel(self, phone: str, today: Optional[date] = None) -> str:
        today = today or date.today()
        signup = self._current(phone)
        if signup is None:
            return "Ik vind geen abonnement bij dit nummer."
        if signup.status == CANCELED:
            return f"Je abonnement was al opgezegd; de bot werkt tot en met {signup.access_until}."
        sub = await self.mollie.get_subscription(signup.customer_id, signup.subscription_id)
        # Paid up to the day before the next charge (or the end of the trial).
        nxt = sub.get("nextPaymentDate") or sub.get("startDate")
        until = (date.fromisoformat(nxt) - timedelta(days=1)) if nxt else today
        await self.mollie.cancel_subscription(signup.customer_id, signup.subscription_id)
        signup.status = CANCELED
        signup.access_until = max(until, today).isoformat()
        signup.note(f"cancelled by customer, access until {signup.access_until}")
        self.store.save(signup)
        await self._tell_admins(f"{signup.name} ({signup.phone}) heeft opgezegd, actief t/m {signup.access_until}.")
        return (
            f"Je abonnement is opgezegd. Er wordt niets meer afgeschreven. De bot werkt "
            f"tot en met {signup.access_until}. Stuur vóór die datum 'excel all' om al je "
            "ritten als Excel te bewaren."
        )

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
        return (
            "Je abonnement is afgelopen, dus ik registreer geen ritten meer. "
            f"Opnieuw beginnen kan op {self.settings.signup_url}"
        )

    async def _tell_admins(self, text: str) -> None:
        for admin in self.settings.admin_list():
            try:
                await self.notify(admin, text)
            except Exception:  # pragma: no cover - a notification must not break billing
                log.exception("admin notification failed")
