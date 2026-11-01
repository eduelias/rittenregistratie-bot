"""The public website: landing page, signup, Mollie return and webhook.

Mounted by ``main.py``. Pages are plain HTML files in ``site/`` filled with
:class:`string.Template` (``$name``); everything a customer typed is escaped.
Without ``RIT_MOLLIE_API_KEY`` and ``RIT_PUBLIC_BASE_URL`` the landing page
still renders but the form is replaced by a notice, and ``/aanmelden`` is 404.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections import deque
from pathlib import Path
from string import Template
from typing import Awaitable, Callable, Deque, Dict, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .billing import SignupError as _SignupError, parse_phone
from .billing import (
    ACTIVE, ATTENTION, CANCELED, ENDED, FAILED, PAUSED, PENDING, Billing, MollieError, SignupError,
)
from .analytics import PageCounter
from .config import Settings

log = logging.getLogger("rittenregistratie.site")

SITE_DIR = Path(__file__).parent / "site"

NAME = "RittenRegistratie-bot"
DESCRIPTION = ("Houd je rittenregistratie bij via WhatsApp. Stuur je kilometerstand en bestemming, "
               "ontvang een Excel met alle velden die de Belastingdienst vraagt. 14 dagen gratis.")

# Files under site/ a browser may fetch from /static/<name>, with their media type.
# The favicon is du7's own (as on du7.dev); the bot icon is its WhatsApp profile picture.
STATIC = {
    "site.css": "text/css",
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/x-icon",
    "apple-touch-icon.png": "image/png",
    "bot-icon-96.png": "image/png",
    "bot-icon-192.png": "image/png",
    "bot-icon-512.png": "image/png",
    "og-card.png": "image/png",
}


def _nl(value: str) -> str:
    return f"{float(value):.2f}".replace(".", ",")


def _read(name: str) -> str:
    return (SITE_DIR / name).read_text(encoding="utf-8")


async def _form(request: Request) -> dict:
    """An urlencoded body as a dict. Both the signup form and Mollie's webhook
    are plain urlencoded posts, so this saves a python-multipart dependency."""
    body = (await request.body())[:20_000].decode("utf-8", "replace")
    return dict(parse_qsl(body, keep_blank_values=True))


class SignupLimiter:
    """Signup attempts per client IP per hour, and in total per hour.

    Every form post creates a Mollie customer, so an unthrottled form lets
    anyone fill the Mollie account with junk. In memory: a restart resets it,
    which is fine for this. The client IP is what uvicorn reports; behind
    Caddy on 127.0.0.1 that is X-Forwarded-For (uvicorn trusts it from
    127.0.0.1 by default)."""

    WINDOW = 3600.0

    def __init__(self, per_ip: int, total: int, clock=time.monotonic):
        self.per_ip, self.total, self._clock = per_ip, total, clock
        self._by_ip: Dict[str, Deque[float]] = {}
        self._all: Deque[float] = deque()

    def _trim(self, q: Deque[float], now: float) -> None:
        while q and now - q[0] > self.WINDOW:
            q.popleft()

    def allow(self, ip: str) -> bool:
        now = self._clock()
        q = self._by_ip.setdefault(ip or "?", deque())
        self._trim(q, now)
        self._trim(self._all, now)
        if len(q) >= self.per_ip or len(self._all) >= self.total:
            return False
        q.append(now)
        self._all.append(now)
        return True


class CancelCodes:
    """One-time codes for the website's cancel button, in memory.

    A code is 6 digits, lives 10 minutes, allows 5 tries, and only its hash is
    kept. At most 3 codes per number per hour, so the form cannot be used to
    flood someone's WhatsApp."""

    TTL, TRIES, PER_HOUR = 600.0, 5, 3

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._codes: Dict[str, list] = {}       # phone -> [hash, expiry, tries]
        self._sent: Dict[str, Deque[float]] = {}

    @staticmethod
    def _hash(phone: str, code: str) -> str:
        return hashlib.sha256(f"{phone}:{code}".encode()).hexdigest()

    def issue(self, phone: str) -> Optional[str]:
        now = self._clock()
        sent = self._sent.setdefault(phone, deque())
        while sent and now - sent[0] > 3600:
            sent.popleft()
        if len(sent) >= self.PER_HOUR:
            return None
        sent.append(now)
        code = f"{secrets.randbelow(10**6):06d}"
        self._codes[phone] = [self._hash(phone, code), now + self.TTL, 0]
        return code

    def check(self, phone: str, code: str) -> bool:
        entry = self._codes.get(phone)
        if entry is None:
            return False
        if self._clock() > entry[1] or entry[2] >= self.TRIES:
            self._codes.pop(phone, None)
            return False
        entry[2] += 1
        if hmac.compare_digest(entry[0], self._hash(phone, (code or "").strip())):
            self._codes.pop(phone, None)
            return True
        return False


SendCode = Callable[[str, str], Awaitable[bool]]
ConfirmCancel = Callable[[str, str, str], Awaitable[bool]]


def build_router(settings: Settings, billing: Optional[Billing], *,
                 send_code: Optional[SendCode] = None,
                 confirm_cancel: Optional[ConfirmCancel] = None) -> APIRouter:
    router = APIRouter()

    def values(**extra) -> dict:
        v = {
            "price_monthly_nl": _nl(settings.price_monthly),
            "price_yearly_nl": _nl(settings.price_yearly),
            "mandate_nl": _nl(settings.mandate_amount),
            "trial_days": str(settings.trial_days),
            "support_email": html.escape(settings.support_email),
            "company_line": html.escape(settings.company_line),
            "retention_days": str(settings.retention_days),
        }
        v.update(extra)
        return v

    base = settings.public_base_url.rstrip("/")
    counter = PageCounter(settings.analytics_file, urlsplit(base).hostname or "")

    def count(request: Request, path: str) -> None:
        """Cookieless daily totals only; see analytics.py. Never breaks a page."""
        try:
            counter.view(path, user_agent=request.headers.get("user-agent", ""),
                         referer=request.headers.get("referer", ""), query=request.url.query)
        except Exception:  # pragma: no cover - statistics are best effort
            log.exception("page count failed")

    def head_meta(title: str, path: str) -> str:
        """Icons, theme colour and the tags link previews (WhatsApp, LinkedIn,
        search engines) read. Absolute URLs need RIT_PUBLIC_BASE_URL."""
        e = html.escape
        url, card = f"{base}{path}", f"{base}/static/og-card.png"
        tags = [
            f'<meta name="description" content="{e(DESCRIPTION)}">',
            '<link rel="icon" href="/favicon.ico" sizes="32x32">',
            '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">',
            '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
            '<meta name="theme-color" content="#128C7E">',
            '<meta property="og:type" content="website">',
            f'<meta property="og:site_name" content="{NAME}">',
            '<meta property="og:locale" content="nl_NL">',
            f'<meta property="og:title" content="{e(title)}">',
            f'<meta property="og:description" content="{e(DESCRIPTION)}">',
            f'<meta property="og:image" content="{e(card)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            f'<meta property="og:image:alt" content="{NAME}: je rittenregistratie, gewoon via WhatsApp">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
        if base:
            tags += [f'<link rel="canonical" href="{e(url)}">',
                     f'<meta property="og:url" content="{e(url)}">']
        return "\n".join(tags)

    def json_ld() -> str:
        """schema.org data for search engines: who sells what, for how much."""
        org = {"@type": "Organization", "@id": f"{base}/#du7", "name": "Du7 Technology Services",
               "url": "https://du7.dev"}
        if settings.support_email:
            org["email"] = settings.support_email
        app = {
            "@type": "SoftwareApplication", "name": NAME, "url": f"{base}/",
            "description": DESCRIPTION, "inLanguage": "nl",
            "applicationCategory": "BusinessApplication", "operatingSystem": "WhatsApp",
            "image": f"{base}/static/bot-icon-512.png", "provider": {"@id": org["@id"]},
            "offers": [
                {"@type": "Offer", "name": "Maandelijks", "price": f"{float(settings.price_monthly):.2f}",
                 "priceCurrency": "EUR"},
                {"@type": "Offer", "name": "Jaarlijks", "price": f"{float(settings.price_yearly):.2f}",
                 "priceCurrency": "EUR"},
            ],
        }
        data = {"@context": "https://schema.org", "@graph": [org, app]}
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    def page(title: str, body: str, status: int = 200, *, robots: str = "noindex",
             head_extra: str = "", path: str = "") -> HTMLResponse:
        filled = Template(body).safe_substitute(values())
        return HTMLResponse(
            Template(_read("page.html")).safe_substitute(values(
                title=html.escape(title), body=filled, robots=robots, head_extra=head_extra,
                head_meta=head_meta(f"{title} · {NAME}", path),
            )),
            status_code=status,
        )

    @router.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> HTMLResponse:
        count(request, "/")
        if billing is None:
            block = ('<p class="notice">Aanmelden kan binnenkort. Laat je e-mailadres achter via '
                     f'<a href="mailto:{html.escape(settings.support_email)}">'
                     f'{html.escape(settings.support_email)}</a>.</p>')
        elif not billing.has_room():
            block = '<p class="notice">We zitten op dit moment vol. Probeer het binnenkort opnieuw.</p>'
        else:
            block = Template(_read("form.html")).safe_substitute(values())
        return HTMLResponse(Template(_read("index.html")).safe_substitute(values(
            signup_block=block, json_ld=json_ld(),
            head_meta=head_meta(f"{NAME}: rittenregistratie via WhatsApp", "/"),
        )))

    @router.get("/static/{name}")
    async def static(name: str) -> Response:
        media = STATIC.get(name)
        if media is None:
            return Response(status_code=404)
        return FileResponse(SITE_DIR / name, media_type=media,
                            headers={"Cache-Control": "public, max-age=86400"})

    @router.get("/favicon.ico")
    async def favicon() -> FileResponse:
        return FileResponse(SITE_DIR / "favicon.ico", media_type="image/x-icon")

    @router.get("/voorwaarden", response_class=HTMLResponse)
    async def terms(request: Request) -> HTMLResponse:
        count(request, "/voorwaarden")
        return page("Voorwaarden", _read("voorwaarden.html"), robots="index", path="/voorwaarden")

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request) -> HTMLResponse:
        count(request, "/privacy")
        return page("Privacy", _read("privacy.html"), robots="index", path="/privacy")

    @router.get("/herroeping", response_class=HTMLResponse)
    async def withdrawal(request: Request) -> HTMLResponse:
        count(request, "/herroeping")
        return page("Herroeping", _read("herroeping.html"), path="/herroeping")

    if billing is None:
        return router

    limiter = SignupLimiter(settings.signup_limit_per_ip, settings.signup_limit_total)

    @router.post("/aanmelden")
    async def signup(request: Request) -> Response:
        ip = request.client.host if request.client else ""
        if not limiter.allow(ip):
            log.warning("Signup refused by the rate limit (%s).", ip)
            return page("Aanmelden", '<h1>Even rustig aan</h1><p class="error">Er zijn net te veel '
                        'aanmeldingen geprobeerd. Probeer het over een uur opnieuw, of mail ons.</p>',
                        status=429)
        form = await _form(request)
        try:
            counter.form()
        except Exception:  # pragma: no cover
            log.exception("form count failed")
        try:
            checkout = await billing.start(form)
        except SignupError as exc:
            return page("Aanmelden", f'<h1>Bijna</h1><p class="error">{html.escape(str(exc))}</p>'
                        '<p><a class="btn" href="javascript:history.back()">Terug naar het formulier</a></p>',
                        status=400)
        except MollieError:
            log.exception("Mollie refused the signup")
            return page("Aanmelden", '<h1>Er ging iets mis</h1><p class="error">We konden de betaling '
                        'niet starten. Probeer het over een paar minuten opnieuw.</p>', status=502)
        return RedirectResponse(checkout, status_code=303)

    @router.get("/aanmelden/klaar", response_class=HTMLResponse)
    async def done(id: str = "") -> HTMLResponse:
        try:
            signup = await billing.sync_signup(id)
        except MollieError:
            log.exception("Mollie sync on return failed")
            signup = billing.store.get(id)
        if signup is None:
            return page("Aanmelden", "<h1>Onbekende aanmelding</h1><p>Deze link klopt niet.</p>", 404)
        if signup.renew_payment_id and signup.status == ACTIVE:
            return page("Welkom terug", (
                f"<h1>Welkom terug, {html.escape(signup.name.split()[0])}!</h1>"
                "<p>Je machtiging is vernieuwd en je abonnement loopt weer. Stuur in WhatsApp "
                "je kilometerstand en bestemming zoals altijd.</p>"
            ))
        if signup.renew_payment_id and signup.status in (PAUSED, CANCELED, ENDED):
            if signup.renew_failed:
                return page("Niet gelukt", (
                    "<h1>De machtiging is niet gelukt</h1><p>Er is niets afgeschreven. Stuur "
                    "'hervatten' in WhatsApp voor een nieuwe link.</p>"
                ))
            return page("Even geduld", (
                "<h1>Betaling wordt verwerkt</h1><p>Mollie heeft je betaling nog niet bevestigd. "
                "Deze pagina ververst zichzelf.</p>"
            ), head_extra='<meta http-equiv="refresh" content="5">')
        if signup.status == ACTIVE:
            number = settings.whatsapp_public_number
            link = f"https://wa.me/{number}?text={quote('start')}" if number else ""
            button = (f'<p><a class="btn" href="{link}">Open WhatsApp en stuur "start"</a></p>'
                      if link else "")
            return page("Welkom", (
                f"<h1>Welkom, {html.escape(signup.name.split()[0])}!</h1>"
                f"<p>Je nummer +{signup.phone} staat aan voor <strong>{html.escape(signup.car_label)}</strong>, "
                f"beginstand {signup.seed_odometer} km. Je proefperiode duurt $trial_days dagen.</p>"
                f"{button}"
                "<p>Daarna stuur je na elke rit je kilometerstand en bestemming, bijvoorbeeld "
                "<code>45230 Kantoor</code>.</p>"
            ))
        if signup.status == PENDING:
            return page("Even geduld", (
                "<h1>Betaling wordt verwerkt</h1><p>Mollie heeft je betaling nog niet bevestigd. "
                "Deze pagina ververst zichzelf.</p>"
            ), head_extra='<meta http-equiv="refresh" content="5">')
        if signup.status == FAILED:
            return page("Niet gelukt", (
                '<h1>De machtiging is niet gelukt</h1><p>Er is niets afgeschreven. '
                '<a href="/#aanmelden">Probeer het opnieuw</a>.</p>'
            ))
        if signup.status == ATTENTION:
            return page("Bijna klaar", (
                "<h1>Betaling ontvangen</h1><p>We zetten je nummer handmatig aan en sturen je "
                "binnen een werkdag een bericht.</p>"
            ))
        return page("Aanmelding", "<h1>Deze aanmelding is niet meer actief.</h1>")

    @router.post("/billing/mollie/webhook")
    async def mollie_webhook(request: Request) -> Response:
        """Mollie posts ``id=tr_...``. We fetch the payment ourselves, so the
        body is never trusted. Always 200 for ids we do not know (Mollie
        retries non-2xx); 500 on our own failure so Mollie retries."""
        payment_id = (await _form(request)).get("id", "")
        try:
            await billing.sync_payment(payment_id)
        except MollieError:
            log.exception("Mollie webhook sync failed for %s", payment_id)
            return Response(status_code=500)
        return Response(status_code=200)

    # --- cancel button (opzegknop): as easy as signing up -----------------
    codes = CancelCodes()
    cancel_limiter = SignupLimiter(settings.signup_limit_per_ip, settings.signup_limit_total)

    def cancel_page(body: str, status: int = 200) -> HTMLResponse:
        return page("Abonnement opzeggen", body, status, path="/opzeggen")

    def code_form(phone: str, note: str = "") -> str:
        return (
            "<h1>Abonnement opzeggen</h1>"
            + (f'<p class="error">{html.escape(note)}</p>' if note else "")
            + "<p>Als dit nummer een abonnement heeft, hebben we je via WhatsApp een code van 6 "
            "cijfers gestuurd. Vul hem hieronder in. De code is 10 minuten geldig.</p>"
            '<form method="post" action="/opzeggen/bevestig" class="form">'
            f'<input type="hidden" name="phone" value="{html.escape(phone)}">'
            '<label>Code<input name="code" inputmode="numeric" pattern="[0-9]{6}" '
            'maxlength="6" required autocomplete="one-time-code"></label>'
            '<button class="btn" type="submit">Abonnement nu opzeggen</button></form>'
            '<p class="fine">Geen code ontvangen? <a href="/opzeggen">Probeer het opnieuw</a>, '
            "of stuur <code>opzeggen</code> naar de bot.</p>"
        )

    @router.get("/opzeggen", response_class=HTMLResponse)
    async def cancel_start(request: Request) -> HTMLResponse:
        count(request, "/opzeggen")
        return cancel_page(
            "<h1>Abonnement opzeggen</h1>"
            "<p>Vul het WhatsApp-nummer van je abonnement in. We sturen je via WhatsApp een "
            "code; daarmee zeg je meteen op. Er wordt daarna niets meer afgeschreven en de bot "
            "werkt tot het einde van je betaalde periode of proefperiode.</p>"
            '<form method="post" action="/opzeggen" class="form">'
            '<label>WhatsApp-nummer<input name="phone" type="tel" required '
            'placeholder="06 12345678" autocomplete="tel"></label>'
            '<button class="btn" type="submit">Stuur mij een code</button></form>'
            "<p class=\"fine\">Je kunt ook <code>opzeggen</code> naar de bot sturen.</p>"
        )

    @router.post("/opzeggen", response_class=HTMLResponse)
    async def cancel_send(request: Request) -> HTMLResponse:
        ip = request.client.host if request.client else ""
        if not cancel_limiter.allow(ip):
            return cancel_page('<h1>Even rustig aan</h1><p class="error">Te veel pogingen. '
                               'Probeer het over een uur opnieuw.</p>', 429)
        try:
            phone = parse_phone((await _form(request)).get("phone", ""))
        except _SignupError as exc:
            return cancel_page(f'<h1>Abonnement opzeggen</h1><p class="error">{html.escape(str(exc))}</p>'
                               '<p><a class="btn" href="/opzeggen">Terug</a></p>', 400)
        # The same answer whether or not the number has a subscription, so the
        # form does not tell strangers who is a customer.
        if billing is not None and send_code is not None and billing.can_cancel(phone):
            code = codes.issue(phone)
            if code is not None:
                try:
                    await send_code(phone, code)
                except Exception:  # pragma: no cover
                    log.exception("cancel code could not be sent")
        return cancel_page(code_form(phone))

    @router.post("/opzeggen/bevestig", response_class=HTMLResponse)
    async def cancel_confirm(request: Request) -> HTMLResponse:
        form = await _form(request)
        try:
            phone = parse_phone(form.get("phone", ""))
        except _SignupError:
            return cancel_page('<h1>Abonnement opzeggen</h1><p class="error">Onbekend nummer.</p>', 400)
        if billing is None or not codes.check(phone, form.get("code", "")):
            return cancel_page(code_form(phone, "Die code klopt niet of is verlopen."), 400)
        try:
            reply = await billing.cancel(phone)
        except MollieError:
            log.exception("website cancel failed for %s", phone)
            return cancel_page('<h1>Er ging iets mis</h1><p class="error">Opzeggen lukte nu niet. '
                               'Probeer het later opnieuw of stuur <code>opzeggen</code> naar de bot.</p>', 502)
        until = billing.access_until(phone)
        if confirm_cancel is not None:
            try:
                await confirm_cancel(phone, reply, until)
            except Exception:  # pragma: no cover
                log.exception("cancel confirmation could not be sent")
        from .i18n import date_text
        when = (f" De bot werkt nog tot en met <strong>{date_text(until, 'nl')}</strong>; stuur "
                "voor die datum <code>excel all</code> naar de bot om al je ritten te bewaren."
                if until else "")
        return cancel_page(
            "<h1>Je abonnement is opgezegd</h1>"
            f"<p>Er wordt niets meer afgeschreven.{when}</p>"
            "<p>Je krijgt deze bevestiging ook via WhatsApp. Bedacht? Stuur <code>opnieuw</code> "
            "naar de bot.</p>"
        )

    return router
