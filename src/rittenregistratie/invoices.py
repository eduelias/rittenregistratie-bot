"""VAT invoices (facturen) for subscription payments.

Mollie does not send invoices for subscription charges, so the bot writes one
for every paid recurring payment: a PDF with what art. 35a Wet OB asks for
(number, date, supplier with address, KvK and VAT number, customer name and
address, what was delivered and for which period, the amount excluding VAT,
the rate and the VAT amount) and sends it to the subscriber on WhatsApp.

- Numbers run per calendar year without gaps: ``2026-0001``, ``2026-0002``.
  Test-mode payments get their own series (``TEST-2026-0001``), so testing
  never uses up a live number.
- An issued invoice is never changed. New billing details (``bedrijfsnaam``,
  ``factuuradres``) apply to the next invoice.
- Prices on the site include 21% VAT; the invoice splits the amount paid.
- The €0.01 mandate payment gets no invoice.

Stored in ``data/invoices/``: ``invoices.json`` (the register) and one PDF per
invoice, so the daily backup covers them. No email: nothing may be sent as
@rrbot.nl.

    python -m rittenregistratie.invoices list
    python -m rittenregistratie.invoices issue <payment id>   # e.g. a missed webhook
"""
from __future__ import annotations

import argparse
import calendar
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .billing import Signup
from .cars import normalize_phone
from .config import Settings
from .i18n import date_text, money, t

log = logging.getLogger("rittenregistratie.invoices")

VAT_RATE = 21
_CENT = Decimal("0.01")


@dataclass
class Invoice:
    number: str
    issued: str             # ISO date (the payment date)
    payment_id: str
    signup_id: str
    phone: str
    customer: str
    company: str
    address: str
    email: str
    description: str
    period_start: str       # ISO dates, inclusive
    period_end: str
    total: str              # incl. VAT, "4.95"
    net: str
    vat: str
    vat_rate: int = VAT_RATE
    test: bool = False

    @property
    def filename(self) -> str:
        return f"factuur-{self.number}.pdf"


def split_vat(total: str, rate: int = VAT_RATE) -> Tuple[str, str]:
    """An amount including VAT -> (excluding VAT, VAT), to the cent."""
    gross = Decimal(total).quantize(_CENT)
    net = (gross * 100 / (100 + rate)).quantize(_CENT, ROUND_HALF_UP)
    return str(net), str(gross - net)


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def period(start: date, interval: str) -> Tuple[date, date]:
    """The service period a charge on ``start`` pays for (Mollie interval
    ``1 month`` / ``12 months``), inclusive."""
    n = int((re.match(r"\s*(\d+)", interval or "") or [None, "1"])[1])
    months = n * 12 if "year" in (interval or "") else n
    return start, add_months(start, months) - timedelta(days=1)


def _day(stamp: str) -> Optional[date]:
    """A Mollie timestamp (UTC) as a date in the Netherlands."""
    try:
        moment = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None
    if moment is None:
        return None
    return moment.astimezone(ZoneInfo("Europe/Amsterdam")).date() if moment.tzinfo else moment.date()


class InvoiceStore:
    """``data/invoices/``: ``invoices.json`` {number: Invoice} and the PDFs."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.path = directory / "invoices.json"

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8")) or {}

    def all(self) -> List[Invoice]:
        return [Invoice(**v) for v in self._load().values()]

    def get(self, number: str) -> Optional[Invoice]:
        raw = self._load().get(number)
        return Invoice(**raw) if raw else None

    def for_payment(self, payment_id: str) -> Optional[Invoice]:
        return next((i for i in self.all() if i.payment_id == payment_id), None)

    def for_phone(self, phone: str) -> List[Invoice]:
        phone = normalize_phone(phone)
        return sorted((i for i in self.all() if i.phone == phone),
                      key=lambda i: (i.issued, i.number))

    def pdf(self, invoice: Invoice) -> Path:
        return self.dir / invoice.filename

    def next_number(self, year: int, test: bool) -> str:
        prefix = f"{'TEST-' if test else ''}{year}-"
        used = [int(n[len(prefix):]) for n in self._load() if n.startswith(prefix)
                and n[len(prefix):].isdigit()]
        return f"{prefix}{max(used, default=0) + 1:04d}"

    def add(self, invoice: Invoice, pdf: bytes) -> None:
        """Write the PDF, then the register entry (a crash in between leaves
        an orphan PDF, never a number without its file)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        data = self._load()
        if invoice.number in data:
            raise ValueError(f"invoice {invoice.number} exists")
        self.pdf(invoice).write_bytes(pdf)
        data[invoice.number] = asdict(invoice)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


# --- the PDF ------------------------------------------------------------------

def supplier_lines(settings: Settings) -> List[str]:
    """Name, street, postcode + place, KvK, VAT number, email; from
    RIT_COMPANY_NAME and RIT_COMPANY_LINE."""
    lines = [settings.company_name]
    kvk = vat = ""
    for part in (p.strip() for p in settings.company_line.split("·")):
        addr = re.fullmatch(r"(.+?),\s*(\d{4}\s?[A-Z]{2}\s+.+)", part)
        if addr:
            lines += [addr.group(1), addr.group(2)]
        if re.fullmatch(r"KvK\s*\d{8}", part, re.I):
            kvk = part
        if re.fullmatch(r"btw\s*NL\d{9}B\d{2}", part, re.I):
            vat = "Btw-nummer " + part.split()[-1]
    lines += [x for x in (kvk, vat, settings.support_email) if x]
    return lines


def _eur(value: str) -> str:
    return "€ " + money(value, "nl")


def render_pdf(invoice: Invoice, settings: Settings) -> bytes:
    from fpdf import FPDF

    pdf = FPDF(format="A4")
    pdf.core_fonts_encoding = "windows-1252"  # € and accents in the core fonts
    pdf.set_margins(20, 20, 20)
    pdf.set_auto_page_break(True, 20)
    pdf.add_page()
    pdf.set_title(f"Factuur {invoice.number}")
    pdf.set_author(settings.company_name)
    w = pdf.epw

    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(w / 2, 10, "Factuur")
    pdf.set_font("Helvetica", "", 9)
    y = pdf.get_y()
    for line in supplier_lines(settings):
        pdf.set_xy(20 + w / 2, y)
        pdf.cell(w / 2, 4.6, line, align="R")
        y += 4.6
    if invoice.test:
        pdf.set_xy(20, 32)
        pdf.set_text_color(200, 0, 0)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(w / 2, 6, "TESTFACTUUR: geen geldige factuur")
        pdf.set_text_color(0, 0, 0)

    pdf.set_y(max(y, 48) + 8)
    top = pdf.get_y()
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(w / 2, 5, "Aan", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    to = [invoice.company, invoice.customer if not invoice.company else f"t.a.v. {invoice.customer}"]
    to += [part.strip() for part in invoice.address.split(",") if part.strip()] if invoice.address else []
    for line in (x for x in to if x):
        pdf.cell(w / 2, 5, line, new_x="LMARGIN", new_y="NEXT")
    bottom = pdf.get_y()

    meta = [("Factuurnummer", invoice.number), ("Factuurdatum", date_text(invoice.issued, "nl")),
            ("Betaald", f"{date_text(invoice.issued, 'nl')} via Mollie")]
    pdf.set_y(top)
    for label, value in meta:
        pdf.set_x(20 + w / 2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(w / 4, 5, label)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(w / 4, 5, value, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(bottom, pdf.get_y()) + 12)

    cols = (w * 0.50, w * 0.30, w * 0.20)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(238, 241, 236)
    for width, head, align in zip(cols, ("Omschrijving", "Periode", "Bedrag excl. btw"),
                                  ("L", "L", "R")):
        pdf.cell(width, 7, head, align=align, fill=True)
    pdf.ln(7)
    pdf.set_font("Helvetica", "", 10)
    span = (f"{date.fromisoformat(invoice.period_start):%d-%m-%Y} t/m "
            f"{date.fromisoformat(invoice.period_end):%d-%m-%Y}")
    size = 10.0
    while pdf.get_string_width(invoice.description) > cols[0] - 2 and size > 7:
        size -= 0.5
        pdf.set_font("Helvetica", "", size)
    pdf.cell(cols[0], 7, invoice.description)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(cols[1], 7, span)
    pdf.cell(cols[2], 7, _eur(invoice.net), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.line(20, pdf.get_y() + 1, 20 + w, pdf.get_y() + 1)
    pdf.ln(5)

    for label, value, bold in (("Totaal excl. btw", invoice.net, False),
                               (f"Btw {invoice.vat_rate}% over {_eur(invoice.net)}", invoice.vat, False),
                               ("Totaal incl. btw", invoice.total, True)):
        pdf.set_font("Helvetica", "B" if bold else "", 10)
        pdf.set_x(20 + cols[0])
        pdf.cell(cols[1], 6, label)
        pdf.cell(cols[2], 6, _eur(value), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(w, 4.6,
                   f"Dit bedrag is al betaald via Mollie (betaling {invoice.payment_id}); "
                   "je hoeft niets over te maken.\n"
                   f"Vragen over deze factuur: {settings.support_email or settings.company_name}.")
    return bytes(pdf.output())


# --- issuing ------------------------------------------------------------------

class Invoicing:
    """Issues invoices for paid subscription payments and answers the
    invoice commands. ``mollie`` is the Billing's client (for the interval)."""

    def __init__(self, settings: Settings, mollie=None):
        self.settings = settings
        self.mollie = mollie
        self.store = InvoiceStore(settings.invoices_dir)

    async def _interval(self, signup: Signup, payment: dict, mollie) -> str:
        sid = payment.get("subscriptionId") or ""
        if mollie is not None and sid and signup.customer_id:
            try:
                sub = await mollie.get_subscription(signup.customer_id, sid)
                if sub.get("interval"):
                    return sub["interval"]
            except Exception:  # the plan on file is the fallback
                log.warning("Could not read subscription %s for its interval.", sid)
        return "12 months" if signup.plan == "yearly" else "1 month"

    async def issue(self, signup: Signup, payment: dict, mollie=None) -> Optional[Invoice]:
        """The invoice for one paid subscription payment; the existing one when
        it was issued before. None for anything else. The caller serialises
        calls (Billing's lock), which keeps the numbers gapless."""
        if payment.get("status") != "paid" or not payment.get("subscriptionId"):
            return None
        existing = self.store.for_payment(payment["id"])
        if existing:
            return existing
        interval = await self._interval(signup, payment, mollie or self.mollie)
        paid = _day(payment.get("paidAt", "")) or date.today()
        start = _day(payment.get("createdAt", "")) or paid
        first, last = period(start, interval)
        total = (payment.get("amount") or {}).get("value") or "0.00"
        net, vat = split_vat(total)
        test = payment.get("mode") == "test"
        plan = "jaarabonnement" if "12" in interval or "year" in interval else "maandabonnement"
        invoice = Invoice(
            number=self.store.next_number(paid.year, test), issued=paid.isoformat(),
            payment_id=payment["id"], signup_id=signup.id, phone=signup.phone,
            customer=signup.name, company=signup.company, address=signup.billing_address,
            email=signup.email,
            description=f"RittenRegistratie-bot, {plan} ({signup.car_label})"[:70],
            period_start=first.isoformat(), period_end=last.isoformat(),
            total=f"{Decimal(total):.2f}", net=net, vat=vat, test=test,
        )
        self.store.add(invoice, render_pdf(invoice, self.settings))
        log.info("Invoice %s issued for payment %s.", invoice.number, payment["id"])
        return invoice


# --- WhatsApp commands --------------------------------------------------------

_GET = re.compile(r"^/?(?:factuur|invoice)(?:\s+(\S+))?$", re.I)
_LIST = re.compile(r"^/?(?:facturen|invoices)$", re.I)
_COMPANY = re.compile(r"^/?(?:bedrijfsnaam|company)(?:\s+(.+))?$", re.I | re.S)
_ADDRESS = re.compile(r"^/?(?:factuuradres|billing address)(?:\s+(.+))?$", re.I | re.S)
_DETAILS = re.compile(r"^/?(?:factuurgegevens|billing details)$", re.I)


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """(action, argument) for an invoice command, else None. Actions: get,
    list, company, address, details. A '-' argument clears a detail."""
    text = (text or "").strip()
    for action, rx in (("get", _GET), ("list", _LIST), ("company", _COMPANY),
                       ("address", _ADDRESS), ("details", _DETAILS)):
        m = rx.match(text)
        if m:
            return action, (m.group(1) or "").strip() if m.groups() else ""
    return None


def details_text(signup: Signup) -> str:
    none = t("inv.none_set")
    return t("inv.details", name=signup.name, company=signup.company or none,
             address=signup.billing_address or none)


def caption(invoice: Invoice, lang: Optional[str] = None) -> str:
    return t("inv.caption", lang, number=invoice.number, amount=money(invoice.total, lang),
             start=date_text(invoice.period_start, lang), end=date_text(invoice.period_end, lang))


def main(argv: Optional[List[str]] = None) -> int:
    import asyncio

    from .billing import Mollie, SignupStore
    from .config import get_settings
    ap = argparse.ArgumentParser(description="VAT invoices for subscription payments.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="print the register")
    issue = sub.add_parser("issue", help="issue the invoice for a paid subscription payment")
    issue.add_argument("payment_id")
    args = ap.parse_args(argv)
    settings = get_settings()
    if args.cmd == "list":
        for i in sorted(InvoiceStore(settings.invoices_dir).all(), key=lambda i: i.number):
            print(f"{i.number}  {i.issued}  €{i.total:>7}  {i.phone}  {i.payment_id}  "
                  f"{i.period_start}..{i.period_end}")
        return 0
    mollie = Mollie(settings.mollie_api_key, settings.mollie_api_url)
    payment = asyncio.run(mollie.get_payment(args.payment_id))
    signup = SignupStore(settings.signups_file).find(subscription_id=payment.get("subscriptionId", ""))
    if signup is None:
        print("No signup for that payment's subscription.")
        return 1
    invoice = asyncio.run(Invoicing(settings, mollie).issue(signup, payment))
    if invoice is None:
        print(f"No invoice: payment is {payment.get('status')}, not a paid subscription payment.")
        return 1
    print(f"{invoice.number}: {InvoiceStore(settings.invoices_dir).pdf(invoice)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
