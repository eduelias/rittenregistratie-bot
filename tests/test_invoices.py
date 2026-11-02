"""VAT invoices for subscription payments (invoices.py)."""

BOT_LANGUAGE = "nl"
import re
import zlib
from datetime import date

import pytest

from rittenregistratie.billing import Billing
from rittenregistratie.engine import Engine
from rittenregistratie.invoices import (
    InvoiceStore, Invoicing, parse_command, period, split_vat, supplier_lines,
)
from test_billing import FORM, FakeMollie, _settings, _wa, app  # noqa: F401  (fixture)

COMPANY = "Buitenhof 128, 1354 GT Almere · KvK 91476003 · btw NL004896802B29"


def pdf_text(data: bytes) -> bytes:
    """The decompressed content streams of a PDF, for substring checks."""
    out = b""
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out += zlib.decompress(raw)
        except zlib.error:
            out += raw
    return out


def test_vat_split_and_periods():
    assert split_vat("4.95") == ("4.09", "0.86")
    assert split_vat("49.00") == ("40.50", "8.50")
    assert split_vat("0.01") == ("0.01", "0.00")
    assert period(date(2026, 11, 15), "1 month") == (date(2026, 11, 15), date(2026, 12, 14))
    assert period(date(2026, 11, 15), "12 months") == (date(2026, 11, 15), date(2027, 11, 14))
    assert period(date(2027, 1, 31), "1 month")[1] == date(2027, 2, 27)
    from rittenregistratie.invoices import _day
    assert _day("2026-11-30T23:30:00+00:00") == date(2026, 12, 1)   # Amsterdam time
    assert _day("") is None and _day("nonsense") is None


def test_commands():
    assert parse_command("factuur") == ("get", "")
    assert parse_command("Factuur 2026-0003") == ("get", "2026-0003")
    assert parse_command("invoices") == ("list", "")
    assert parse_command("bedrijfsnaam Vries & Zn BV") == ("company", "Vries & Zn BV")
    assert parse_command("factuuradres Laan 5, 3511 AB Utrecht") == ("address", "Laan 5, 3511 AB Utrecht")
    assert parse_command("billing details") == ("details", "")
    assert parse_command("45230 Bedrijfsweg 5") is None
    assert parse_command("factuurtje") is None


def test_supplier_lines(tmp_path):
    lines = supplier_lines(_settings(tmp_path, company_line=COMPANY, support_email="support@rrbot.nl"))
    assert lines == ["Du7 Technology Services", "Buitenhof 128", "1354 GT Almere", "KvK 91476003",
                     "Btw-nummer NL004896802B29", "support@rrbot.nl"]


@pytest.fixture
def paid(tmp_path):
    """An active subscriber with invoicing wired in like main.py does."""
    settings = _settings(tmp_path, company_line=COMPANY, support_email="support@rrbot.nl")
    mollie = FakeMollie()
    invoicing = Invoicing(settings, mollie)
    issued = []

    async def on_paid(signup, payment):
        issued.append(await invoicing.issue(signup, payment))

    async def notify(phone, text):
        return True

    billing = Billing(settings, Engine(settings), mollie, notify, on_paid=on_paid)
    return billing, mollie, invoicing, issued


async def _subscriber(billing, mollie, **form):
    await billing.start(dict(FORM, **form))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    return await billing.sync_payment(signup.first_payment_id)


def _charge(mollie, signup, pid, status="paid", mode="live", value="4.95"):
    mollie.payments[pid] = {"id": pid, "status": status, "mode": mode,
                            "subscriptionId": signup.subscription_id, "sequenceType": "recurring",
                            "amount": {"value": value, "currency": "EUR"},
                            "createdAt": "2026-11-16T05:00:00+00:00",
                            "paidAt": "2026-11-18T09:30:00+00:00"}


async def test_paid_charge_gets_one_numbered_invoice(paid):
    billing, mollie, invoicing, issued = paid
    signup = await _subscriber(billing, mollie, company="De Vries Advies",
                               billing_address="Laan 5, 3511 AB Utrecht")
    assert signup.company == "De Vries Advies"
    assert not issued                                   # the mandate payment gets none
    _charge(mollie, signup, "tr_50")
    await billing.sync_payment("tr_50")
    await billing.sync_payment("tr_50")                 # webhook retry
    _charge(mollie, signup, "tr_51", status="failed")
    await billing.sync_payment("tr_51")
    _charge(mollie, signup, "tr_52")
    await billing.sync_payment("tr_52")
    numbers = [i.number for i in invoicing.store.all()]
    assert sorted(numbers) == ["2026-0001", "2026-0002"]
    inv = invoicing.store.for_payment("tr_50")
    assert (inv.issued, inv.period_start, inv.period_end) == ("2026-11-18", "2026-11-16", "2026-12-15")
    assert (inv.total, inv.net, inv.vat, inv.vat_rate) == ("4.95", "4.09", "0.86", 21)
    assert inv.company == "De Vries Advies" and inv.customer == "Anna de Vries"
    text = pdf_text(invoicing.store.pdf(inv).read_bytes())
    for needle in (b"Factuur", b"2026-0001", b"18 november 2026", b"Du7 Technology Services",
                   b"Buitenhof 128", b"1354 GT Almere", b"KvK 91476003", b"NL004896802B29",
                   b"De Vries Advies", b"Anna de Vries", b"3511 AB Utrecht", b"maandabonnement",
                   b"16-11-2026 t/m 15-12-2026", b"\x80 4,09", b"Btw 21%", b"\x80 0,86",
                   b"\x80 4,95", b"tr_50"):
        assert needle in text, needle
    assert b"TESTFACTUUR" not in text


async def test_test_mode_payments_have_their_own_series(paid):
    billing, mollie, invoicing, _ = paid
    signup = await _subscriber(billing, mollie)
    _charge(mollie, signup, "tr_60", mode="test", value="49.00")
    mollie.subs[signup.subscription_id]["interval"] = "12 months"
    await billing.sync_payment("tr_60")
    inv = invoicing.store.for_payment("tr_60")
    assert inv.number == "TEST-2026-0001" and inv.test and inv.period_end == "2027-11-15"
    assert "jaarabonnement" in inv.description
    assert InvoiceStore(invoicing.store.dir).next_number(2026, test=False) == "2026-0001"
    assert b"TESTFACTUUR" in pdf_text(invoicing.store.pdf(inv).read_bytes())


async def test_an_invoice_error_alerts_the_admin_and_keeps_billing_going(tmp_path):
    settings = _settings(tmp_path)
    mollie, sent = FakeMollie(), []

    async def notify(phone, text):
        sent.append((phone, text))
        return True

    async def broken(signup, payment):
        raise RuntimeError("disk full")

    billing = Billing(settings, Engine(settings), mollie, notify, on_paid=broken)
    signup = await _subscriber(billing, mollie)
    _charge(mollie, signup, "tr_70")
    assert (await billing.sync_payment("tr_70")).id == signup.id
    assert any("tr_70" in text and "factuur" in text for _, text in sent)


# --- over WhatsApp ----------------------------------------------------------

def _fake_whatsapp(main, monkeypatch, window_open=True):
    out = {"text": [], "docs": [], "templates": []}

    async def send_message(token, pid, to, body, **kw):
        out["text"].append((to, str(body)))
        return True

    async def upload_media(token, pid, path, **kw):
        return f"media:{path.name}"

    async def send_document(token, pid, to, media_id, filename, caption="", **kw):
        out["docs"].append((to, filename, caption))
        return True

    async def send_template(token, pid, to, name, params, language="", **kw):
        out["templates"].append((to, name, params, language))
        return True

    for name, fn in (("send_message", send_message), ("upload_media", upload_media),
                     ("send_document", send_document), ("send_template", send_template)):
        monkeypatch.setattr(main.whatsapp, name, fn)
    monkeypatch.setattr(main._window, "is_open", lambda phone, now=None: window_open)
    return out


def _subscribe_over_http(client, main, fake):
    client.post("/aanmelden", data=dict(FORM, company="De Vries Advies"), follow_redirects=False)
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    return main._billing.store.get(signup.id)


def test_invoice_reaches_the_subscriber(app, monkeypatch):
    client, main, fake = app
    main._invoicing = Invoicing(main._settings)
    out = _fake_whatsapp(main, monkeypatch, window_open=False)
    signup = _subscribe_over_http(client, main, fake)
    phone = signup.phone
    client.post("/webhook", json=_wa("factuur", phone))
    assert "nog geen factuur" in out["text"][-1][1]

    _charge(fake, signup, "tr_80")
    client.post("/billing/mollie/webhook", data={"id": "tr_80"})
    # Window closed: the template, with the amount and the number.
    assert out["templates"][-1] == (phone, "invoice_ready", ["€ 4,95", "2026-0001"], "nl")
    assert not out["docs"]

    # The subscriber replies 'factuur' (window open now): the PDF.
    monkeypatch.setattr(main._window, "is_open", lambda phone, now=None: True)
    client.post("/webhook", json=_wa("factuur", phone))
    assert out["docs"][-1][:2] == (phone, "factuur-2026-0001.pdf")
    assert "€ 4,95" in out["docs"][-1][2]
    client.post("/webhook", json=_wa("factuur 2026-0009", phone))
    assert "2026-0009" in out["text"][-1][1]
    client.post("/webhook", json=_wa("facturen", phone))
    assert "2026-0001" in out["text"][-1][1]


def test_billing_details_over_whatsapp(app, monkeypatch):
    client, main, fake = app
    main._invoicing = Invoicing(main._settings)
    out = _fake_whatsapp(main, monkeypatch)
    phone = _subscribe_over_http(client, main, fake).phone
    client.post("/webhook", json=_wa("factuurgegevens", phone))
    assert "De Vries Advies" in out["text"][-1][1] and "(niet ingevuld)" in out["text"][-1][1]
    client.post("/webhook", json=_wa("factuuradres Laan 5, 3511 AB Utrecht", phone))
    assert "Opgeslagen" in out["text"][-1][1]
    client.post("/webhook", json=_wa("bedrijfsnaam -", phone))
    signup = main._billing.signup_for(phone)
    assert (signup.company, signup.billing_address) == ("", "Laan 5, 3511 AB Utrecht")
    # A number without a subscription: not an invoice command, so no details.
    client.post("/webhook", json=_wa("factuurgegevens", "31699999999"))
    assert "Laan 5" not in out["text"][-1][1]


# --- WhatsApp health and the weekly report ------------------------------------

async def test_whatsapp_health_alerts_on_drop_and_tier_change(tmp_path):
    import httpx

    from rittenregistratie import wa_health
    from rittenregistratie.analytics import report

    settings = _settings(tmp_path, whatsapp_token="tok", whatsapp_phone_number_id="123")
    readings = iter([
        {"quality_rating": "GREEN", "whatsapp_business_manager_messaging_limit": "TIER_2K"},
        {"quality_rating": "GREEN", "whatsapp_business_manager_messaging_limit": "TIER_2K"},
        {"quality_rating": "YELLOW", "whatsapp_business_manager_messaging_limit": "TIER_250"},
        {"quality_rating": "GREEN", "messaging_limit_tier": "TIER_250"},
    ])
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=dict(next(readings), display_phone_number="+1 555"))

    alerts = []

    async def alert(key, **kw):
        alerts.append((key, kw))

    transport = httpx.MockTransport(handler)
    for _ in range(4):
        await wa_health.check(settings, alert, transport)
    assert "quality_rating" in seen[0].url.params["fields"]
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert [k for k, _ in alerts] == ["alert.wa_quality", "alert.wa_tier"]   # the rise is quiet
    assert alerts[0][1] == {"number": "+1 555", "old": "GREEN", "new": "YELLOW"}
    text = report(settings, today=date(2026, 11, 2))
    assert "WhatsApp: quality GREEN, messaging limit TIER_250" in text
    assert "Invoices: 0 issued" in text


async def test_report_counts_live_invoices_only(paid):
    from rittenregistratie.analytics import report

    billing, mollie, invoicing, _ = paid
    signup = await _subscriber(billing, mollie)
    _charge(mollie, signup, "tr_90")
    _charge(mollie, signup, "tr_91", mode="test")
    await billing.sync_payment("tr_90")
    await billing.sync_payment("tr_91")
    text = report(billing.settings, today=date(2026, 11, 20))
    assert "Invoices: 1 issued, €4.95 incl. VAT (€0.86 VAT)" in text
    assert "WhatsApp: not checked yet" in text
