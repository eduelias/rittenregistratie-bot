"""Subscriptions: signup -> first payment -> subscription -> car, and after."""

# These tests check the Dutch customer texts (the default language).
BOT_LANGUAGE = "nl"
import asyncio
import importlib
import re
import json
from datetime import date, timedelta

import httpx
import pytest
import yaml

from rittenregistratie.billing import (
    ACTIVE, ATTENTION, CANCELED, ENDED, FAILED, PENDING, Billing, Mollie, MollieError, SignupError,
    parse_phone,
)
from rittenregistratie.config import Settings
from rittenregistratie.engine import Engine

ADMIN = "31600000001"
FORM = {
    "name": "Anna de Vries", "email": "anna@example.nl", "phone": "06 1234 5678",
    "car_label": "Golf", "seed_odometer": "45.192", "seed_address": "Laan 5, Utrecht",
    "plan": "monthly", "terms": "on",
}


class FakeMollie:
    """In-memory stand-in for the Mollie calls Billing makes."""

    def __init__(self):
        self.payments, self.subs, self.calls = {}, {}, []
        self.mandates = [{"id": "mdt_1", "status": "valid", "method": "directdebit",
                          "details": {"consumerName": "A de Vries",
                                      "consumerAccount": "NL55INGB0001231234"}}]
        self.fail_subscriptions = False
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}_{self._n}"

    async def create_customer(self, name, email, metadata):
        self.calls.append(("customer", name))
        return {"id": self._id("cst")}

    async def create_payment(self, body):
        pid = self._id("tr")
        self.payments[pid] = {"id": pid, "status": "open", "sequenceType": body["sequenceType"],
                              "mandateId": "mdt_1", "metadata": body["metadata"],
                              "_links": {"checkout": {"href": f"https://mollie.test/{pid}"}}}
        self.calls.append(("payment", body))
        return self.payments[pid]

    async def get_payment(self, pid):
        if pid not in self.payments:
            raise MollieError("not found", 404)
        return self.payments[pid]

    async def create_subscription(self, customer_id, body):
        if self.fail_subscriptions:
            raise MollieError("refused", 422)
        sid = self._id("sub")
        self.subs[sid] = {"id": sid, "status": "active", "startDate": body["startDate"],
                          "nextPaymentDate": body["startDate"], "amount": body["amount"],
                          "interval": body["interval"], "mandateId": body.get("mandateId")}
        self.calls.append(("subscription", body))
        return self.subs[sid]

    async def get_subscription(self, customer_id, sid):
        return self.subs[sid]

    async def cancel_subscription(self, customer_id, sid):
        self.subs[sid]["status"] = "canceled"
        self.calls.append(("cancel", sid))
        return self.subs[sid]

    async def list_mandates(self, customer_id):
        return list(self.mandates)


def _settings(tmp_path, **kw):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "locations.yaml").write_text("home: {address: 'Buitenhof 128'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        f"mercedes:\n  label: Mercedes\n  seed_odometer: 1\n  phones: ['{ADMIN}']\n"
        "  event_plugins: [reallocate]\n")
    base = dict(data_dir=tmp_path / "data", config_dir=cfg, trajectory_provider="maps_link",
                private_cap_plugin="warn", whatsapp_app_secret="", admin_numbers=ADMIN,
                mollie_api_key="test_x", public_base_url="https://rrbot.example", max_users=5)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def setup(tmp_path):
    settings = _settings(tmp_path)
    engine = Engine(settings)
    mollie = FakeMollie()
    sent = []

    async def notify(phone, text):
        sent.append((phone, text))
        return True

    return Billing(settings, engine, mollie, notify), mollie, engine, sent


def _cars(billing):
    return yaml.safe_load(billing.settings.cars_file.read_text())


async def test_signup_creates_a_first_payment(setup):
    billing, mollie, _, _ = setup
    url = await billing.start(dict(FORM))
    assert url.startswith("https://mollie.test/tr_")
    body = mollie.calls[1][1]
    assert body["sequenceType"] == "first"
    assert body["amount"] == {"currency": "EUR", "value": "0.01"}
    assert body["webhookUrl"] == "https://rrbot.example/billing/mollie/webhook"
    assert body["redirectUrl"].startswith("https://rrbot.example/aanmelden/klaar?id=")
    signup = billing.store.all()[0]
    assert signup.status == PENDING and signup.phone == "31612345678"
    assert signup.seed_odometer == 45192


async def test_paid_first_payment_provisions_a_plugin_free_car(setup):
    billing, mollie, engine, sent = setup
    await billing.start(dict(FORM))
    pid = billing.store.all()[0].first_payment_id
    mollie.payments[pid]["status"] = "paid"
    signup = await billing.sync_payment(pid)
    assert signup.status == ACTIVE
    entry = _cars(billing)[signup.car_id]
    assert entry["phones"] == ["31612345678"]
    assert entry["event_plugins"] == [] and entry["cap_plugin"] == "warn"
    assert entry["subscription"] == signup.id
    assert engine.cars.get(signup.car_id).is_subscriber
    sub_body = [c for c in mollie.calls if c[0] == "subscription"][0][1]
    assert sub_body["amount"]["value"] == "4.95" and sub_body["interval"] == "1 month"
    assert sub_body["startDate"] == (date.today() + timedelta(days=14)).isoformat()
    assert sub_body["mandateId"] == "mdt_1"
    assert sent and sent[0][0] == ADMIN and "Nieuwe abonnee" in sent[0][1]


async def test_webhook_and_return_page_do_not_double_provision(setup):
    billing, mollie, _, _ = setup
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    await billing.sync_payment(signup.first_payment_id)
    await billing.sync_signup(signup.id)
    await billing.sync_payment(signup.first_payment_id)
    assert len([c for c in mollie.calls if c[0] == "subscription"]) == 1
    assert len(_cars(billing)) == 2


async def test_yearly_plan_amount(setup):
    billing, mollie, _, _ = setup
    await billing.start(dict(FORM, plan="yearly"))
    pid = billing.store.all()[0].first_payment_id
    mollie.payments[pid]["status"] = "paid"
    await billing.sync_payment(pid)
    body = [c for c in mollie.calls if c[0] == "subscription"][0][1]
    assert body["amount"]["value"] == "49.00" and body["interval"] == "12 months"


async def test_failed_first_payment_adds_nothing(setup):
    billing, mollie, _, _ = setup
    await billing.start(dict(FORM))
    pid = billing.store.all()[0].first_payment_id
    mollie.payments[pid]["status"] = "expired"
    assert (await billing.sync_payment(pid)).status == FAILED
    assert len(_cars(billing)) == 1
    assert not [c for c in mollie.calls if c[0] == "subscription"]


async def test_number_already_taken_needs_attention(setup):
    billing, mollie, _, sent = setup
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    # Someone registered the number between the form and the payment.
    billing.engine.handle_admin_command("approve", ["31612345678", "Other", "10"])
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    assert (await billing.sync_payment(signup.first_payment_id)).status == ATTENTION
    assert "kon niet worden ingericht" in sent[-1][1]


@pytest.mark.parametrize("field,value", [
    ("email", "nope"), ("phone", "12"), ("seed_odometer", "abc"), ("plan", "weekly"),
    ("terms", ""), ("name", " "), ("car_label", ""), ("seed_address", ""),
])
def test_validation_rejects_bad_input(setup, field, value):
    billing = setup[0]
    with pytest.raises(SignupError):
        billing.validate(dict(FORM, **{field: value}))


def test_validation_rejects_a_registered_number(setup):
    with pytest.raises(SignupError):
        setup[0].validate(dict(FORM, phone="+" + ADMIN))


def test_parse_phone():
    assert parse_phone("06-12345678") == "31612345678"
    assert parse_phone("+31 6 1234 5678") == "31612345678"
    assert parse_phone("0031612345678") == "31612345678"
    assert parse_phone("+32 470 12 34 56") == "32470123456"


async def test_full_house_refuses_before_payment(tmp_path):
    settings = _settings(tmp_path, max_users=1)
    billing = Billing(settings, Engine(settings), FakeMollie(), None)
    assert not billing.has_room()
    with pytest.raises(SignupError):
        await billing.start(dict(FORM))


async def test_cancel_keeps_access_until_paid_period_ends(setup):
    billing, mollie, engine, _ = setup
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    await billing.sync_payment(signup.first_payment_id)
    reply = await billing.cancel("31612345678")
    assert "opgezegd" in reply
    signup = billing.store.get(signup.id)
    trial_end = date.today() + timedelta(days=13)
    assert signup.status == CANCELED and signup.access_until == trial_end.isoformat()
    assert ("cancel", signup.subscription_id) in mollie.calls
    # Still works on the last day, removed the day after; the ledger stays.
    assert billing.expired("31612345678", today=trial_end) is None
    assert "afgelopen" in billing.expired("31612345678", today=trial_end + timedelta(days=1))
    assert not engine.cars.is_registered("31612345678")
    assert billing.store.get(signup.id).status == ENDED


async def test_failed_recurring_payment_tells_the_admin_once(setup):
    billing, mollie, _, sent = setup
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    signup = await billing.sync_payment(signup.first_payment_id)
    mollie.payments["tr_99"] = {"id": "tr_99", "status": "failed",
                                "subscriptionId": signup.subscription_id}
    await billing.sync_payment("tr_99")
    await billing.sync_payment("tr_99")
    alerts = [t for _, t in sent if "tr_99" in t]
    assert len(alerts) == 1 and "failed" in alerts[0]
    assert billing.engine.cars.is_registered("31612345678")  # admin decides


async def test_unknown_or_foreign_payment_ids_are_ignored(setup):
    billing = setup[0]
    assert await billing.sync_payment("") is None
    assert await billing.sync_payment("../customers") is None


async def test_local_base_url_sends_no_webhook(tmp_path):
    settings = _settings(tmp_path, public_base_url="http://127.0.0.1:8000")
    mollie = FakeMollie()
    await Billing(settings, Engine(settings), mollie, None).start(dict(FORM))
    assert "webhookUrl" not in mollie.calls[1][1]


async def test_mollie_client_wire_format():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(201, json={"id": "tr_1"})

    m = Mollie("test_abc", transport=httpx.MockTransport(handler))
    assert m.test_mode
    await m.create_payment({"amount": {"currency": "EUR", "value": "0.01"}})
    await m.cancel_subscription("cst_1", "sub_1")
    assert seen[0].url == "https://api.mollie.com/v2/payments"
    assert seen[0].headers["Authorization"] == "Bearer test_abc"
    assert json.loads(seen[0].content)["amount"]["value"] == "0.01"
    assert seen[1].method == "DELETE"
    assert seen[1].url.path == "/v2/customers/cst_1/subscriptions/sub_1"


# --- over HTTP and WhatsApp -------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "locations.yaml").write_text("{}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        f"mercedes:\n  label: M\n  seed_odometer: 1\n  phones: ['{ADMIN}']\n")
    for k, v in {
        "RIT_CONFIG_DIR": str(cfg), "RIT_DATA_DIR": str(tmp_path / "data"),
        "RIT_WHATSAPP_APP_SECRET": "", "RIT_WHATSAPP_TOKEN": "", "RIT_WHATSAPP_PHONE_NUMBER_ID": "",
        "RIT_MOLLIE_API_KEY": "test_x", "RIT_PUBLIC_BASE_URL": "https://rrbot.example",
        "RIT_WHATSAPP_PUBLIC_NUMBER": "15553361591", "RIT_ADMIN_NUMBERS": ADMIN,
    }.items():
        monkeypatch.setenv(k, v)
    import rittenregistratie.config as config
    config._settings = None
    import rittenregistratie.main as main
    importlib.reload(main)
    fake = FakeMollie()
    main._billing.mollie = fake
    from fastapi.testclient import TestClient
    return TestClient(main.app), main, fake


def _wa(text, sender):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "type": "text", "text": {"body": text}}]}}]}]}


def test_landing_page_and_legal_pages(app):
    client = app[0]
    page = client.get("/").text
    assert "4,95" in page and "49,00" in page and 'action="/aanmelden"' in page
    assert client.get("/voorwaarden").status_code == 200
    assert client.get("/privacy").status_code == 200
    assert "Modelformulier" in client.get("/herroeping").text
    assert "Concept" not in client.get("/voorwaarden").text
    assert client.get("/static/site.css").headers["content-type"].startswith("text/css")


def test_signup_to_whatsapp_over_http(app):
    client, main, fake = app
    resp = client.post("/aanmelden", data=FORM, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("https://mollie.test/")
    signup = main._billing.store.all()[0]
    # Customer returns before Mollie confirms.
    assert "wordt verwerkt" in client.get(f"/aanmelden/klaar?id={signup.id}").text
    fake.payments[signup.first_payment_id]["status"] = "paid"
    assert client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id}).status_code == 200
    done = client.get(f"/aanmelden/klaar?id={signup.id}").text
    assert "Welkom" in done and "wa.me/15553361591" in done
    # The new number is now served, with the Dutch welcome.
    assert main._engine.cars.is_registered("31612345678")
    assert client.post("/webhook", json=_wa("start", "31612345678")).status_code == 200


def test_bad_form_is_explained(app):
    resp = app[0].post("/aanmelden", data=dict(FORM, email="x"))
    assert resp.status_code == 400 and "e-mailadres" in resp.text


def test_unknown_number_gets_the_signup_link(app, monkeypatch):
    client, main, _ = app
    sent = []

    async def fake_send(token, pid, to, body, **kw):
        sent.append((to, str(body)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_message", fake_send)
    client.post("/webhook", json=_wa("hallo", "31699999999"))
    assert sent and "https://rrbot.example/#aanmelden" in sent[0][1]
    assert not main._engine.onboarding.has("31699999999")


def test_webhook_with_unknown_id_is_ok(app):
    # Mollie retries anything but 2xx; an id it does not know is not worth a retry.
    assert app[0].post("/billing/mollie/webhook", data={"id": "tr_nope"}).status_code == 200


def test_icons_meta_and_structured_data(app):
    import json as _json
    import re
    client = app[0]
    for name, kind in [("bot-icon-96.png", "image/png"), ("og-card.png", "image/png"),
                       ("favicon.svg", "image/svg+xml")]:
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind)
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/static/site.py").status_code == 404
    assert client.get("/static/..%2Fsite.py").status_code == 404
    page = client.get("/").text
    assert '<meta property="og:image" content="https://rrbot.example/static/og-card.png">' in page
    assert '<link rel="canonical" href="https://rrbot.example/">' in page
    assert 'src="/static/bot-icon-96.png"' in page and 'href="https://du7.dev"' in page
    ld = _json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
    offers = ld["@graph"][1]["offers"]
    assert [o["price"] for o in offers] == ["4.95", "49.00"]
    terms = client.get("/voorwaarden").text
    assert '<link rel="canonical" href="https://rrbot.example/voorwaarden">' in terms
    assert terms.count('<meta name="description"') == 1


def test_pages_are_counted_and_admin_gets_stats(app, monkeypatch):
    import json as _json
    client, main, _ = app
    ua = {"user-agent": "Mozilla/5.0 Safari", "referer": "https://www.google.com/"}
    client.get("/?utm_campaign=test", headers=ua)
    client.get("/privacy", headers=ua)
    client.get("/", headers={"user-agent": "facebookexternalhit/1.1"})
    client.post("/aanmelden", data=dict(FORM, email="x"))       # invalid, still a form post
    days = _json.loads(main._settings.analytics_file.read_text())
    (day,) = days.values()
    assert day["views"] == {"/": 1, "/privacy": 1} and day["bots"] == 1
    assert day["referrers"] == {"google.com": 2} and day["campaigns"] == {"test": 1}
    assert day["forms"] == 1
    sent = []

    async def fake_send(token, pid, to, body, **kw):
        sent.append((to, str(body)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_message", fake_send)
    client.post("/webhook", json=_wa("stats 30", ADMIN))
    assert sent and sent[0][0] == ADMIN and "last 30 days" in sent[0][1]
    sent.clear()
    client.post("/webhook", json=_wa("stats", "31699999999"))   # not an admin
    assert not any("page views" in b for _, b in sent)


async def test_admin_alert_in_the_admin_language_customer_text_in_theirs(setup):
    billing, mollie, engine, sent = setup
    engine.languages.set(ADMIN, "en")
    await billing.start(dict(FORM))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    await billing.sync_payment(signup.first_payment_id)
    assert sent[-1][0] == ADMIN and sent[-1][1].startswith("New subscriber: Anna de Vries")
    assert "monthly" in sent[-1][1]
    reply = await billing.handle("31612345678", "abonnement")
    assert reply.startswith("Je abonnement is actief: maandelijks, € 4,95 per maand.")
    engine.languages.set("31612345678", "en")
    reply = await billing.handle("31612345678", "subscription")
    assert reply.startswith("Your subscription is active: monthly, € 4.95 per month.")
    assert "Reply with the number of your choice:" in reply


def _activate_over_http(client, main, fake):
    client.post("/aanmelden", data=FORM, follow_redirects=False)
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    return signup


def test_cancel_button_with_whatsapp_code(app, monkeypatch):
    client, main, fake = app
    _activate_over_http(client, main, fake)
    codes, templates = [], []

    async def fake_code(token, pid, to, name, code, language="en", graph_url=""):
        codes.append((to, name, code, language))
        return True

    async def fake_template(token, pid, to, name, params, language="en", graph_url=""):
        templates.append((to, name, params, language))
        return True

    monkeypatch.setattr(main.whatsapp, "send_auth_code", fake_code)
    monkeypatch.setattr(main.whatsapp, "send_template", fake_template)
    assert "Abonnement opzeggen" in client.get("/").text            # footer link
    assert 'action="/opzeggen"' in client.get("/opzeggen").text
    page = client.post("/opzeggen", data={"phone": "06 1234 5678"}).text
    assert "code van 6" in page and 'value="31612345678"' in page
    (to, name, code, lang), = codes
    assert (to, name, lang) == ("31612345678", "cancel_code", "nl") and len(code) == 6
    bad = client.post("/opzeggen/bevestig", data={"phone": "31612345678", "code": "000000"
                                                  if code != "000000" else "111111"})
    assert bad.status_code == 400 and "klopt niet" in bad.text
    ok = client.post("/opzeggen/bevestig", data={"phone": "31612345678", "code": code})
    assert ok.status_code == 200 and "Je abonnement is opgezegd" in ok.text
    assert any(c[0] == "cancel" for c in fake.calls)
    # The customer never wrote to the bot, so the confirmation is the template.
    assert templates[-1][:2] == ("31612345678", "subscription_cancelled")
    assert re.match(r"\d+ \w+ \d{4}", templates[-1][2][0])
    # A used code does not work twice.
    assert client.post("/opzeggen/bevestig", data={"phone": "31612345678", "code": code}).status_code == 400


def test_cancel_form_does_not_reveal_who_is_a_customer(app, monkeypatch):
    client, main, fake = app
    sent = []

    async def fake_code(*a, **kw):
        sent.append(a)
        return True

    monkeypatch.setattr(main.whatsapp, "send_auth_code", fake_code)
    page = client.post("/opzeggen", data={"phone": "06 9999 9999"}).text
    assert "code van 6" in page and not sent


def test_cancel_codes_expire_and_are_limited():
    from rittenregistratie.site import CancelCodes
    now = [0.0]
    codes = CancelCodes(clock=lambda: now[0])
    first = codes.issue("316")
    assert codes.issue("316") and codes.issue("316") and codes.issue("316") is None
    now[0] = 601
    assert not codes.check("316", first)
    now[0] = 4000
    c = codes.issue("316")
    for _ in range(5):
        codes.check("316", "999999" if c != "999999" else "000000")
    assert not codes.check("316", c)                        # out of tries


def test_admin_alerts_use_the_template_until_the_admin_writes(app, monkeypatch):
    client, main, fake = app
    templates, texts = [], []

    async def fake_template(token, pid, to, name, params, language="en", graph_url=""):
        templates.append((to, name, params, language))
        return True

    async def fake_send(token, pid, to, body, **kw):
        texts.append((to, str(body)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_template", fake_template)
    monkeypatch.setattr(main.whatsapp, "send_message", fake_send)
    asyncio.run(main._alert(ADMIN, "Nieuwe abonnee: X\nregel 2"))
    assert templates[-1] == (ADMIN, "owner_alert", ["Nieuwe abonnee: X · regel 2"], "nl")
    client.post("/webhook", json=_wa("ping", ADMIN))        # the admin writes: window open
    asyncio.run(main._alert(ADMIN, "Nieuwe abonnee: Y"))
    assert texts[-1] == (ADMIN, "Nieuwe abonnee: Y") and len(templates) == 1
