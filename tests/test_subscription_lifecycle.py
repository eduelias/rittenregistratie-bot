"""Subscriber self-service over WhatsApp: status menu, cancel, pause, resume,
restart after the end, plan switch. Builds on the fakes in test_billing."""

# These tests check the Dutch customer texts (the default language).
BOT_LANGUAGE = "nl"
from datetime import date, timedelta

import pytest

from rittenregistratie.billing import ACTIVE, ATTENTION, CANCELED, ENDED, PAUSED, Billing
from rittenregistratie.engine import Engine
from test_billing import ADMIN, FORM, FakeMollie, _cars, _settings, _wa, app, setup  # noqa: F401

PHONE = "31612345678"
TODAY = date.today()
FIRST_CHARGE = TODAY + timedelta(days=14)


async def _subscribed(billing, mollie, **form):
    await billing.start(dict(FORM, **form))
    signup = next(s for s in billing.store.all() if s.status == "pending")
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    return await billing.sync_payment(signup.first_payment_id)


def _subs(mollie):
    return [c[1] for c in mollie.calls if c[0] == "subscription"]


# --- status and menu --------------------------------------------------------

async def test_menu_shows_plan_trial_next_charge_and_mandate(setup):
    billing, mollie, _, _ = setup
    signup = await _subscribed(billing, mollie)
    assert signup.trial_until == (FIRST_CHARGE - timedelta(days=1)).isoformat()
    assert signup.mandate_id == "mdt_1"
    text = await billing.handle(PHONE, "Abonnement")
    assert "actief: maandelijks, € 4,95 per maand" in text
    assert "Proefperiode tot en met" in text
    assert f"Volgende betaling: {FIRST_CHARGE.day} " in text and "€ 4,95" in text
    assert "SEPA-incasso van rekening eindigend op 1234" in text
    assert "1. Overstappen naar jaarlijks (€ 49,00 per jaar)" in text
    assert "2. Pauzeren" in text and "3. Opzeggen" in text


async def test_card_mandate_and_no_mandate_are_described(setup):
    billing, mollie, _, _ = setup
    await _subscribed(billing, mollie)
    mollie.mandates = [{"id": "mdt_2", "status": "valid", "method": "creditcard",
                        "details": {"cardLabel": "Visa", "cardNumber": "4242"}}]
    assert "Visa eindigend op 4242" in await billing.handle(PHONE, "abonnement")
    mollie.mandates = [{"id": "mdt_3", "status": "invalid", "method": "directdebit"}]
    assert "geen geldige machtiging" in await billing.handle(PHONE, "abonnement")


async def test_numbers_only_count_after_a_menu(setup):
    billing, mollie, _, _ = setup
    await _subscribed(billing, mollie)
    assert await billing.handle(PHONE, "2") is None          # no menu: a trip/other text
    await billing.handle(PHONE, "abonnement")
    assert "1 tot en met 3" in await billing.handle(PHONE, "7")
    assert "gepauzeerd" in await billing.handle(PHONE, "2")
    assert await billing.handle(PHONE, "2") is None          # the menu is used up
    assert await billing.handle(PHONE, "45230 Kantoor") is None


async def test_menu_expires(setup, monkeypatch):
    billing, mollie, _, _ = setup
    await _subscribed(billing, mollie)
    await billing.handle(PHONE, "abonnement")
    import rittenregistratie.billing as b
    later = b.time.monotonic() + b.MENU_TTL_S + 1
    monkeypatch.setattr(b.time, "monotonic", lambda: later)
    assert await billing.handle(PHONE, "3") is None


async def test_no_subscription(setup):
    billing = setup[0]
    assert "geen abonnement" in await billing.handle(ADMIN, "abonnement")
    assert "rrbot.example" in await billing.handle(ADMIN, "opzeggen")


# --- plan switch ------------------------------------------------------------

async def test_switch_to_yearly_from_the_menu(setup):
    billing, mollie, _, sent = setup
    signup = await _subscribed(billing, mollie)
    old = signup.subscription_id
    await billing.handle(PHONE, "abonnement")
    reply = await billing.handle(PHONE, "1")
    assert "jaarlijks" in reply and "€ 49,00 per jaar" in reply
    assert ("cancel", old) in mollie.calls
    new = _subs(mollie)[-1]
    assert new["amount"]["value"] == "49.00" and new["interval"] == "12 months"
    assert new["startDate"] == FIRST_CHARGE.isoformat() and new["mandateId"] == "mdt_1"
    signup = billing.store.get(signup.id)
    assert signup.status == ACTIVE and signup.plan == "yearly"
    assert signup.old_subscription_ids == [old]
    assert "stapt over naar jaarlijks" in sent[-1][1] and sent[-1][0] == ADMIN
    # Switching back by word.
    assert "maandelijks" in await billing.handle(PHONE, "maandelijks")
    assert _subs(mollie)[-1]["interval"] == "1 month"
    assert "al een maandelijks" in await billing.handle(PHONE, "maandelijks")


async def test_old_subscription_payments_still_reach_the_admin(setup):
    billing, mollie, _, sent = setup
    signup = await _subscribed(billing, mollie)
    old = signup.subscription_id
    await billing.handle(PHONE, "jaarlijks")
    mollie.payments["tr_late"] = {"id": "tr_late", "status": "failed", "subscriptionId": old}
    assert (await billing.sync_payment("tr_late")).id == signup.id
    assert "tr_late" in sent[-1][1]


async def test_no_switch_or_pause_on_the_charge_day(setup):
    billing, mollie, _, _ = setup
    signup = await _subscribed(billing, mollie)
    mollie.subs[signup.subscription_id]["nextPaymentDate"] = TODAY.isoformat()
    assert "morgen" in await billing.handle(PHONE, "jaarlijks")
    assert "morgen" in await billing.handle(PHONE, "pauzeren")
    assert billing.store.get(signup.id).status == ACTIVE
    assert not [c for c in mollie.calls if c[0] == "cancel"]


async def test_switch_that_fails_after_the_cancel_pauses(setup):
    billing, mollie, _, sent = setup
    signup = await _subscribed(billing, mollie)
    mollie.fail_subscriptions = True
    assert "pauze" in await billing.handle(PHONE, "jaarlijks")
    signup = billing.store.get(signup.id)
    assert signup.status == PAUSED and signup.paid_until == signup.trial_until
    assert "mislukte" in sent[-1][1]
    assert billing.paused(PHONE) is None        # paid period still runs


async def test_switch_needs_a_mandate(setup):
    billing, mollie, _, _ = setup
    await _subscribed(billing, mollie)
    mollie.mandates = []
    assert "niet meer geldig" in await billing.handle(PHONE, "jaarlijks")
    assert not [c for c in mollie.calls if c[0] == "cancel"]


# --- pause / resume ---------------------------------------------------------

async def test_pause_keeps_the_paid_period_then_refuses_trips(setup):
    billing, mollie, engine, sent = setup
    signup = await _subscribed(billing, mollie)
    reply = await billing.handle(PHONE, "pauzeren")
    assert "gepauzeerd" in reply and "niets meer afgeschreven" in reply
    signup = billing.store.get(signup.id)
    assert signup.status == PAUSED and signup.paid_until == signup.trial_until
    assert ("cancel", signup.subscription_id) in mollie.calls
    assert engine.cars.is_registered(PHONE)          # car and ledger stay
    assert "gepauzeerd" in sent[-1][1] and sent[-1][0] == ADMIN
    last_paid = date.fromisoformat(signup.paid_until)
    assert billing.paused(PHONE, today=last_paid) is None
    assert "hervatten" in billing.paused(PHONE, today=last_paid + timedelta(days=1))
    assert billing.expired(PHONE, today=last_paid + timedelta(days=30)) is None
    assert "al gepauzeerd" in await billing.handle(PHONE, "pauze")
    menu = await billing.handle(PHONE, "abonnement")
    assert "1. Hervatten, maandelijks" in menu and "3. Definitief opzeggen" in menu


async def test_resume_within_the_paid_period_starts_after_it(setup):
    billing, mollie, _, sent = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "pauzeren")
    reply = await billing.handle(PHONE, "hervatten")
    assert "loopt weer" in reply and "SEPA-incasso" in reply
    new = _subs(mollie)[-1]
    assert new["startDate"] == FIRST_CHARGE.isoformat() and new["mandateId"] == "mdt_1"
    signup = billing.store.get(signup.id)
    assert signup.status == ACTIVE and signup.paid_until == ""
    assert "gaat verder" in sent[-1][1]


async def test_resume_after_the_paid_period_charges_today_with_the_other_plan(setup):
    billing, mollie, _, _ = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "pauzeren")
    later = FIRST_CHARGE + timedelta(days=40)
    await billing.handle(PHONE, "abonnement", today=later)
    reply = await billing.handle(PHONE, "2", today=later)     # resume, yearly
    assert "jaarlijks" in reply and "vandaag" in reply
    new = _subs(mollie)[-1]
    assert new["startDate"] == later.isoformat() and new["interval"] == "12 months"
    assert billing.store.get(signup.id).plan == "yearly"


async def test_cancel_while_paused(setup):
    billing, mollie, _, _ = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "pauzeren")
    cancels = len([c for c in mollie.calls if c[0] == "cancel"])
    assert "opgezegd" in await billing.handle(PHONE, "opzeggen")
    signup = billing.store.get(signup.id)
    assert signup.status == CANCELED and signup.access_until == signup.paid_until
    assert len([c for c in mollie.calls if c[0] == "cancel"]) == cancels   # nothing left in Mollie


# --- cancel / restart -------------------------------------------------------

async def test_undo_a_cancel(setup):
    billing, mollie, engine, _ = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "opzeggen")
    assert "al opgezegd" in await billing.handle(PHONE, "opzeggen")
    menu = await billing.handle(PHONE, "abonnement")
    assert "1. Opnieuw starten, maandelijks" in menu
    assert "loopt weer" in await billing.handle(PHONE, "1")
    assert _subs(mollie)[-1]["startDate"] == FIRST_CHARGE.isoformat()
    signup = billing.store.get(signup.id)
    assert signup.status == ACTIVE and signup.access_until == ""
    assert billing.expired(PHONE, today=FIRST_CHARGE + timedelta(days=5)) is None


async def test_restart_after_the_end_reuses_customer_mandate_and_car(setup):
    billing, mollie, engine, sent = setup
    signup = await _subscribed(billing, mollie)
    car_id = signup.car_id
    billing.settings.raw_ledger_file(car_id).parent.mkdir(parents=True, exist_ok=True)
    billing.settings.raw_ledger_file(car_id).write_text('{"odo": 45200}\n')
    await billing.handle(PHONE, "opzeggen")
    assert billing.expired(PHONE, today=FIRST_CHARGE)
    assert not engine.cars.is_registered(PHONE) and billing.returning(PHONE)
    customers = len([c for c in mollie.calls if c[0] == "customer"])
    assert "afgelopen" in await billing.handle(PHONE, "abonnement")
    reply = await billing.handle(PHONE, "2")                  # restart yearly
    assert "loopt weer" in reply and "vandaag" in reply
    assert len([c for c in mollie.calls if c[0] == "customer"]) == customers
    assert _subs(mollie)[-1]["startDate"] == TODAY.isoformat()
    assert engine.cars.get(car_id).phones == [PHONE] and engine.cars.get(car_id).is_subscriber
    assert _cars(billing)[car_id]["event_plugins"] == []
    assert billing.settings.raw_ledger_file(car_id).read_text().startswith('{"odo": 45200}')
    assert billing.store.get(signup.id).status == ACTIVE and not billing.returning(PHONE)


async def test_restart_without_mandate_goes_through_a_payment_link(setup):
    billing, mollie, engine, sent = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "opzeggen")
    billing.expired(PHONE, today=FIRST_CHARGE)
    mollie.mandates = []
    reply = await billing.handle(PHONE, "opnieuw")
    assert "€ 0,01" in reply and "https://mollie.test/tr_" in reply
    signup = billing.store.get(signup.id)
    assert signup.status == ENDED and signup.renew_payment_id
    body = [c[1] for c in mollie.calls if c[0] == "payment"][-1]
    assert body["customerId"] == signup.customer_id and body["sequenceType"] == "first"
    assert body["redirectUrl"].endswith(f"klaar?id={signup.id}")
    # Asking again while it is open gives the same link, not a second payment.
    again = await billing.handle(PHONE, "jaarlijks")
    assert signup.renew_payment_id in again
    assert billing.store.get(signup.id).renew_plan == "yearly"
    # Paid (return page first, then the webhook): restart once.
    mollie.payments[signup.renew_payment_id].update(status="paid", mandateId="mdt_9")
    await billing.sync_signup(signup.id)
    await billing.sync_payment(signup.renew_payment_id)
    signup = billing.store.get(signup.id)
    assert signup.status == ACTIVE and signup.plan == "yearly" and signup.mandate_id == "mdt_9"
    assert len([s for s in _subs(mollie) if s.get("mandateId") == "mdt_9"]) == 1
    assert engine.cars.is_registered(PHONE)
    assert sent[-1][0] == PHONE and "loopt weer" in sent[-1][1]


async def test_failed_renew_payment_changes_nothing(setup):
    billing, mollie, _, _ = setup
    signup = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "pauzeren")
    mollie.mandates = []
    await billing.handle(PHONE, "hervatten")
    signup = billing.store.get(signup.id)
    mollie.payments[signup.renew_payment_id]["status"] = "expired"
    await billing.sync_payment(signup.renew_payment_id)
    signup = billing.store.get(signup.id)
    assert signup.status == PAUSED and signup.renew_failed
    # A new request makes a new link.
    old = signup.renew_payment_id
    await billing.handle(PHONE, "hervatten")
    assert billing.store.get(signup.id).renew_payment_id != old


async def test_restart_refused_when_full_or_number_taken(tmp_path):
    settings = _settings(tmp_path, max_users=3)
    mollie = FakeMollie()
    billing = Billing(settings, Engine(settings), mollie, None)

    async def quiet(phone, text):
        return True

    billing.notify = quiet
    await _subscribed(billing, mollie)
    await billing.handle(PHONE, "opzeggen")
    billing.expired(PHONE, today=FIRST_CHARGE)
    subs = len(_subs(mollie))
    billing.engine.handle_admin_command("approve", [PHONE, "Elsewhere", "10"])
    assert "andere auto" in await billing.handle(PHONE, "opnieuw")
    billing.engine.handle_admin_command("remove", [PHONE])
    billing.engine.handle_admin_command("approve", ["31688888888", "Other", "10"])
    billing.engine.handle_admin_command("approve", ["31677777777", "Third", "10"])
    assert "vol" in await billing.handle(PHONE, "opnieuw")
    assert len(_subs(mollie)) == subs


async def test_a_new_signup_never_gets_an_ended_car_id(setup):
    billing, mollie, _, _ = setup
    first = await _subscribed(billing, mollie)
    await billing.handle(PHONE, "opzeggen")
    billing.expired(PHONE, today=FIRST_CHARGE)
    second = await _subscribed(billing, mollie, phone="0687654321")
    assert first.car_id == "golf" and second.car_id == "golf_2"


async def test_attention_signup_gets_a_holding_reply(setup):
    billing, mollie, _, _ = setup
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    billing.engine.handle_admin_command("approve", [PHONE, "Other", "10"])
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    assert (await billing.sync_payment(signup.first_payment_id)).status == ATTENTION
    assert "afgerond" in await billing.handle(PHONE, "abonnement")
    assert "afgerond" in await billing.handle(PHONE, "pauzeren")


# --- over WhatsApp ----------------------------------------------------------

@pytest.fixture
def wa(app, monkeypatch):
    client, main, fake = app
    sent = []

    async def fake_send(token, pid, to, body, **kw):
        sent.append((to, str(body)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_message", fake_send)

    def say(text, sender=PHONE):
        sent.clear()
        assert client.post("/webhook", json=_wa(text, sender)).status_code == 200
        return [b for to, b in sent if to == sender]

    return say, main, fake, client


def test_menu_pick_and_paused_trip_over_whatsapp(wa):
    say, main, fake, client = wa
    client.post("/aanmelden", data=FORM, follow_redirects=False)
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    assert "Stuur het nummer" in say("abonnement")[0]
    assert "gepauzeerd" in say("2")[0]
    # Paid period over: trips refused, help still answered.
    s = main._billing.store.get(signup.id)
    s.paid_until = (TODAY - timedelta(days=1)).isoformat()
    main._billing.store.save(s)
    assert "registreer geen nieuwe ritten" in say("45300 Kantoor")[0]
    assert "pauzeren" in say("help")[0]
    assert "loopt weer" in say("hervatten")[0]
    assert main._billing.paused(PHONE) is None


def test_ended_number_can_restart_over_whatsapp(wa):
    say, main, fake, client = wa
    client.post("/aanmelden", data=FORM, follow_redirects=False)
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    s = main._billing.store.get(signup.id)
    s.status, s.access_until = CANCELED, (TODAY - timedelta(days=1)).isoformat()
    main._billing.store.save(s)
    assert "afgelopen" in say("45300 Kantoor")[0]           # removed now
    assert not main._engine.cars.is_registered(PHONE)
    assert "opnieuw" in say("hallo")[0]                       # not the signup link
    assert "loopt weer" in say("opnieuw")[0]
    car_id = main._billing.store.get(signup.id).car_id
    assert main._engine.cars.get(car_id).phones == [PHONE]


def test_renew_return_page(wa):
    say, main, fake, client = wa
    client.post("/aanmelden", data=FORM, follow_redirects=False)
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    say("pauzeren")
    fake.mandates = []
    assert "https://mollie.test/" in say("hervatten")[0]
    rid = main._billing.store.get(signup.id).renew_payment_id
    assert "wordt verwerkt" in client.get(f"/aanmelden/klaar?id={signup.id}").text
    fake.payments[rid]["status"] = "paid"
    assert "Welkom terug" in client.get(f"/aanmelden/klaar?id={signup.id}").text
