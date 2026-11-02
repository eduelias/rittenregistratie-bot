"""The public website: landing page, guides, signup, Mollie return and webhook.

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
import io
import logging
import re
import secrets
import time
from collections import deque
from pathlib import Path
from string import Template
from typing import Awaitable, Callable, Deque, Dict, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .billing import SignupError as _SignupError, parse_phone
from .billing import (
    ACTIVE, ATTENTION, CANCELED, ENDED, FAILED, PAUSED, PENDING, Billing, MollieError, SignupError,
)
from .analytics import PageCounter
from .config import Settings

log = logging.getLogger("rittenregistratie.site")

SITE_DIR = Path(__file__).parent / "site"

NAME = "RittenRegistratie-bot"
# What search results show: the title leads with the search term and stays under 60
# characters (the brand comes from the WebSite node in the JSON-LD), the description
# under 155.
TITLE = "Rittenregistratie via WhatsApp, zonder app of kastje"
DESCRIPTION = ("Rittenregistratie via WhatsApp voor zzp en zakelijke auto: app je kilometerstand en "
               "bestemming, krijg een Excel voor de Belastingdienst. 14 dagen gratis.")

# Guide pages: path -> (file under site/, <title>, meta description). Indexed, listed in
# the sitemap, counted like the landing page.
GUIDES = {
    "/rittenregistratie-eisen": (
        "rittenregistratie-eisen.html",
        "Rittenregistratie: eisen Belastingdienst 2026",
        "Wat moet er in een rittenregistratie? De velden per rit, wanneer hij sluitend is, de "
        "500 km privé-grens en hoe lang je hem bewaart. Met bronnen.",
    ),
    "/rittenregistratie-excel": (
        "rittenregistratie-excel.html",
        "Rittenregistratie in Excel: gratis sjabloon",
        "Gratis Excel-sjabloon voor je rittenregistratie, met de kolommen die de Belastingdienst "
        "per rit vraagt. Plus: zo vul je hem in en zo houd je hem sluitend.",
    ),
}
TEMPLATE_PATH = "/rittenregistratie-excel/sjabloon.xlsx"
TEMPLATE_NAME = "rittenregistratie-sjabloon.xlsx"

# The English site, for expats and international employees with a Dutch company car.
# Files under site/en/. The bot itself speaks English per number (i18n.py).
EN_TITLE = "Dutch mileage log (rittenregistratie) via WhatsApp"
EN_DESCRIPTION = ("Keep your Dutch mileage log (rittenregistratie) in English via WhatsApp and get "
                  "an Excel with the fields the Belastingdienst wants. 14 days free.")
EN_GUIDES = {
    "/en/rittenregistratie-requirements": (
        "en/rittenregistratie-requirements.html",
        "Dutch mileage log rules (rittenregistratie)",
        "What a Dutch trip log (rittenregistratie) must contain, the 500 km private-use limit, "
        "when it counts as complete and how long to keep it. With sources.",
    ),
}
# Dutch page -> its English counterpart: hreflang links and the NL/EN switch. A page
# without a counterpart switches to the other language's homepage.
PAIRS = {"/": "/en/", "/rittenregistratie-eisen": "/en/rittenregistratie-requirements",
         "/voorwaarden": "/en/terms", "/privacy": "/en/privacy"}
_PAIRS_BACK = {en: nl for nl, en in PAIRS.items()}
# Pages in the sitemap besides the guides. /herroeping and the cancel pages stay out
# (noindex); so does everything behind a signup.
SITEMAP = ["/", *GUIDES, "/voorwaarden", "/privacy", "/en/", *EN_GUIDES, "/en/terms", "/en/privacy"]


def lang_of(path: str) -> str:
    return "en" if path == "/en" or path.startswith("/en/") else "nl"


def counterpart(path: str) -> Optional[str]:
    """The same page in the other language, or None."""
    return PAIRS.get(path) or _PAIRS_BACK.get(path)


def switch_link(path: str, lang: str) -> str:
    """The header's language switch: to this page in the other language."""
    if lang == "en":
        return f'<a class="lang" href="{counterpart(path) or "/"}" hreflang="nl" lang="nl">NL</a>'
    return f'<a class="lang" href="{counterpart(path) or "/en/"}" hreflang="en" lang="en">EN</a>'


def company_line_en(line: str) -> str:
    """``RIT_COMPANY_LINE`` for the English pages: 'btw' is 'VAT'."""
    return re.sub(r"\bbtw\b", "VAT", line, flags=re.I)

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


def company_facts(line: str) -> dict:
    """Address, KvK and VAT number out of ``RIT_COMPANY_LINE``
    (``"Buitenhof 128, 1354 GT Almere · KvK 91476003 · btw NL004896802B29"``),
    as schema.org Organization fields. A part that is missing or still a
    placeholder is left out."""
    out: dict = {}
    for part in (p.strip() for p in line.split("·")):
        addr = re.fullmatch(r"(.+?),\s*(\d{4}\s?[A-Z]{2})\s+(.+)", part)
        if addr:
            out["address"] = {"@type": "PostalAddress", "streetAddress": addr.group(1),
                              "postalCode": addr.group(2), "addressLocality": addr.group(3),
                              "addressCountry": "NL"}
        kvk = re.fullmatch(r"KvK\s*(\d{8})", part, re.I)
        if kvk:
            out["identifier"] = {"@type": "PropertyValue", "propertyID": "KvK",
                                 "value": kvk.group(1)}
        vat = re.fullmatch(r"btw\s*(NL\d{9}B\d{2})", part, re.I)
        if vat:
            out["vatID"] = vat.group(1)
    return out


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
            "price_monthly_en": f"{float(settings.price_monthly):.2f}",
            "price_yearly_en": f"{float(settings.price_yearly):.2f}",
            "mandate_en": f"{float(settings.mandate_amount):.2f}",
            "company_line_en": html.escape(company_line_en(settings.company_line)),
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

    def head_meta(title: str, path: str, description: str = DESCRIPTION, lang: str = "nl") -> str:
        """Icons, theme colour and the tags link previews (WhatsApp, LinkedIn,
        search engines) read; hreflang for pages that exist in both languages
        (x-default: Dutch). Absolute URLs need RIT_PUBLIC_BASE_URL."""
        e = html.escape
        url, card = f"{base}{path}", f"{base}/static/og-card.png"
        locale, other = ("en_GB", "nl_NL") if lang == "en" else ("nl_NL", "en_GB")
        alt = (f"{NAME}: your Dutch trip log, simply via WhatsApp" if lang == "en"
               else f"{NAME}: je rittenregistratie, gewoon via WhatsApp")
        tags = [
            f'<meta name="description" content="{e(description)}">',
            '<link rel="icon" href="/favicon.ico" sizes="32x32">',
            '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">',
            '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
            '<meta name="theme-color" content="#128C7E">',
            '<meta property="og:type" content="website">',
            f'<meta property="og:site_name" content="{NAME}">',
            f'<meta property="og:locale" content="{locale}">',
            f'<meta property="og:title" content="{e(title)}">',
            f'<meta property="og:description" content="{e(description)}">',
            f'<meta property="og:image" content="{e(card)}">',
            '<meta property="og:image:width" content="1200">',
            '<meta property="og:image:height" content="630">',
            f'<meta property="og:image:alt" content="{e(alt)}">',
            '<meta name="twitter:card" content="summary_large_image">',
        ]
        if base and path:
            tags += [f'<link rel="canonical" href="{e(url)}">',
                     f'<meta property="og:url" content="{e(url)}">']
        pair = counterpart(path) if path else None
        if pair:
            nl, en = (path, pair) if lang == "nl" else (pair, path)
            tags.append(f'<meta property="og:locale:alternate" content="{other}">')
            if base:
                tags += [f'<link rel="alternate" hreflang="nl" href="{e(base + nl)}">',
                         f'<link rel="alternate" hreflang="en" href="{e(base + en)}">',
                         f'<link rel="alternate" hreflang="x-default" href="{e(base + nl)}">']
        return "\n".join(tags)

    def json_ld(lang: str = "nl") -> str:
        """schema.org data for search engines: who sells what, for how much."""
        en = lang == "en"
        home = f"{base}/en/" if en else f"{base}/"
        org = {"@type": "Organization", "@id": f"{base}/#du7", "name": "Du7 Technology Services",
               "url": "https://du7.dev", "sameAs": ["https://du7.dev"],
               **company_facts(settings.company_line)}
        if settings.support_email:
            org["email"] = settings.support_email
        site = {"@type": "WebSite", "@id": f"{base}/#site", "name": NAME, "url": f"{base}/",
                "inLanguage": ["nl", "en"], "publisher": {"@id": org["@id"]}}
        app = {
            "@type": "SoftwareApplication", "name": NAME, "url": home,
            "description": EN_DESCRIPTION if en else DESCRIPTION, "inLanguage": lang,
            "applicationCategory": "BusinessApplication", "operatingSystem": "WhatsApp",
            "image": f"{base}/static/bot-icon-512.png", "provider": {"@id": org["@id"]},
            "offers": [
                {"@type": "Offer", "name": "Monthly" if en else "Maandelijks",
                 "price": f"{float(settings.price_monthly):.2f}", "priceCurrency": "EUR"},
                {"@type": "Offer", "name": "Yearly" if en else "Jaarlijks",
                 "price": f"{float(settings.price_yearly):.2f}", "priceCurrency": "EUR"},
            ],
        }
        data = {"@context": "https://schema.org", "@graph": [org, app, site]}
        return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    def page(title: str, body: str, status: int = 200, *, robots: str = "noindex",
             head_extra: str = "", path: str = "", description: str = "",
             cta: bool = False, lang: str = "nl") -> HTMLResponse:
        filled = Template(body).safe_substitute(values())
        button = ('<a class="btn small" href="/en/#signup">Try it free</a>' if lang == "en"
                  else '<a class="btn small" href="/#aanmelden">Probeer gratis</a>')
        nav = f"<nav>{switch_link(path, lang)}{button if cta else ''}</nav>"
        description = description or (EN_DESCRIPTION if lang == "en" else DESCRIPTION)
        return HTMLResponse(
            Template(_read("en/page.html" if lang == "en" else "page.html")).safe_substitute(values(
                title=html.escape(title), body=filled, robots=robots, head_extra=head_extra, nav=nav,
                head_meta=head_meta(f"{title} · {NAME}", path, description, lang),
            )),
            status_code=status,
        )

    def landing_page(lang: str) -> HTMLResponse:
        en = lang == "en"
        mail = html.escape(settings.support_email)
        if billing is None:
            block = (f'<p class="notice">Signing up opens soon. Email us at <a href="mailto:{mail}">'
                     f'{mail}</a>.</p>' if en else
                     '<p class="notice">Aanmelden kan binnenkort. Laat je e-mailadres achter via '
                     f'<a href="mailto:{mail}">{mail}</a>.</p>')
        elif not billing.has_room():
            block = ('<p class="notice">We\'re full at the moment. Please try again soon.</p>' if en else
                     '<p class="notice">We zitten op dit moment vol. Probeer het binnenkort opnieuw.</p>')
        else:
            block = Template(_read("en/form.html" if en else "form.html")).safe_substitute(values())
        path, title = ("/en/", EN_TITLE) if en else ("/", TITLE)
        return HTMLResponse(Template(_read("en/index.html" if en else "index.html")).safe_substitute(
            values(signup_block=block, json_ld=json_ld(lang), switch=switch_link(path, lang),
                   title=html.escape(title),
                   head_meta=head_meta(title, path, EN_DESCRIPTION if en else DESCRIPTION, lang))))

    @router.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> HTMLResponse:
        count(request, "/")
        return landing_page("nl")

    @router.get("/en/", response_class=HTMLResponse)
    async def landing_en(request: Request) -> HTMLResponse:
        count(request, "/en/")
        return landing_page("en")

    @router.get("/en")
    async def landing_en_slash() -> RedirectResponse:
        return RedirectResponse("/en/", status_code=308)

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

    @router.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        lines = ["User-agent: *", "Disallow: /aanmelden", "Disallow: /opzeggen",
                 "Disallow: /billing/", "Allow: /"]
        if base:
            lines.append(f"Sitemap: {base}/sitemap.xml")
        return "\n".join(lines) + "\n"

    @router.get("/sitemap.xml")
    async def sitemap() -> Response:
        def entry(p: str) -> str:
            alts = ""
            pair = counterpart(p)
            if pair:
                nl, en = (p, pair) if lang_of(p) == "nl" else (pair, p)
                alts = "".join(f'<xhtml:link rel="alternate" hreflang="{h}" href="{html.escape(base + u)}"/>'
                               for h, u in (("nl", nl), ("en", en), ("x-default", nl)))
            return f"<url><loc>{html.escape(base + p)}</loc>{alts}</url>"
        urls = "".join(entry(p) for p in SITEMAP)
        return Response('<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
                        f'xmlns:xhtml="http://www.w3.org/1999/xhtml">{urls}</urlset>\n',
                        media_type="application/xml")

    def guide_page(path: str, guides: dict):
        name, title, description = guides[path]
        lang = lang_of(path)

        async def show(request: Request) -> HTMLResponse:
            count(request, path)
            return page(title, _read(name), robots="index", path=path, description=description,
                        cta=True, lang=lang)
        return show

    for guides in (GUIDES, EN_GUIDES):
        for path in guides:
            router.add_api_route(path, guide_page(path, guides), methods=["GET"],
                                 response_class=HTMLResponse)

    def not_found(lang: str = "nl") -> HTMLResponse:
        if lang == "en":
            return page("Page not found", (
                "<h1>This page doesn't exist</h1>"
                "<p>The link may be old, or there's a typo in it.</p>"
                '<p><a class="btn" href="/en/">To the homepage</a></p>'
                '<p>Or read <a href="/en/rittenregistratie-requirements">what a Dutch trip log must '
                "contain</a>.</p>"), status=404, cta=True, lang="en")
        return page("Pagina niet gevonden", (
            "<h1>Deze pagina bestaat niet</h1>"
            "<p>Misschien is de link oud of zit er een tikfout in.</p>"
            '<p><a class="btn" href="/">Naar de homepage</a></p>'
            "<p>Of lees verder over <a href=\"/rittenregistratie-eisen\">wat er in een "
            "rittenregistratie moet</a> en <a href=\"/rittenregistratie-excel\">rittenregistratie "
            "in Excel</a>.</p>"), status=404, cta=True)
    router.not_found = not_found  # used by install_not_found()

    @router.get(TEMPLATE_PATH)
    async def excel_template(request: Request) -> Response:
        count(request, TEMPLATE_PATH)
        from .excel_writer import template_workbook
        buf = io.BytesIO()
        template_workbook(f"{base or 'https://rrbot.nl'}/").save(buf)
        return Response(buf.getvalue(), headers={
            "Content-Disposition": f'attachment; filename="{TEMPLATE_NAME}"',
            "Cache-Control": "public, max-age=86400"},
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @router.get("/voorwaarden", response_class=HTMLResponse)
    async def terms(request: Request) -> HTMLResponse:
        count(request, "/voorwaarden")
        return page("Voorwaarden", _read("voorwaarden.html"), robots="index", path="/voorwaarden")

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request) -> HTMLResponse:
        count(request, "/privacy")
        return page("Privacy", _read("privacy.html"), robots="index", path="/privacy")

    @router.get("/en/terms", response_class=HTMLResponse)
    async def terms_en(request: Request) -> HTMLResponse:
        count(request, "/en/terms")
        return page("Terms", _read("en/terms.html"), robots="index", path="/en/terms", lang="en")

    @router.get("/en/privacy", response_class=HTMLResponse)
    async def privacy_en(request: Request) -> HTMLResponse:
        count(request, "/en/privacy")
        return page("Privacy", _read("en/privacy.html"), robots="index", path="/en/privacy",
                    lang="en")

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
        form = await _form(request)
        lang = "en" if form.get("lang") == "en" else "nl"
        en = lang == "en"
        title = "Sign up" if en else "Aanmelden"
        if not limiter.allow(ip):
            log.warning("Signup refused by the rate limit (%s).", ip)
            return page(title, '<h1>Slow down a little</h1><p class="error">Too many signups were just '
                        'tried. Please try again in an hour, or email us.</p>' if en else
                        '<h1>Even rustig aan</h1><p class="error">Er zijn net te veel '
                        'aanmeldingen geprobeerd. Probeer het over een uur opnieuw, of mail ons.</p>',
                        status=429, lang=lang)
        try:
            counter.form()
        except Exception:  # pragma: no cover
            log.exception("form count failed")
        try:
            checkout = await billing.start(form)
        except SignupError as exc:
            back = "Back to the form" if en else "Terug naar het formulier"
            return page(title, f'<h1>{"Almost" if en else "Bijna"}</h1>'
                        f'<p class="error">{html.escape(exc.text(lang))}</p>'
                        f'<p><a class="btn" href="javascript:history.back()">{back}</a></p>',
                        status=400, lang=lang)
        except MollieError:
            log.exception("Mollie refused the signup")
            return page(title, '<h1>Something went wrong</h1><p class="error">We couldn\'t start the '
                        'payment. Please try again in a few minutes.</p>' if en else
                        '<h1>Er ging iets mis</h1><p class="error">We konden de betaling '
                        'niet starten. Probeer het over een paar minuten opnieuw.</p>',
                        status=502, lang=lang)
        return RedirectResponse(checkout, status_code=303)

    # The return page, in the language the customer signed up in.
    DONE = {
        "nl": {
            "unknown": ("Aanmelden", "<h1>Onbekende aanmelding</h1><p>Deze link klopt niet.</p>"),
            "back": ("Welkom terug", "<h1>Welkom terug, {first}!</h1><p>Je machtiging is vernieuwd en je "
                     "abonnement loopt weer. Stuur in WhatsApp je kilometerstand en bestemming zoals "
                     "altijd.</p>"),
            "renew_failed": ("Niet gelukt", "<h1>De machtiging is niet gelukt</h1><p>Er is niets "
                             "afgeschreven. Stuur 'hervatten' in WhatsApp voor een nieuwe link.</p>"),
            "wait": ("Even geduld", "<h1>Betaling wordt verwerkt</h1><p>Mollie heeft je betaling nog niet "
                     "bevestigd. Deze pagina ververst zichzelf.</p>"),
            "welcome": ("Welkom", "<h1>Welkom, {first}!</h1><p>Je nummer +{phone} staat aan voor "
                        "<strong>{car}</strong>, beginstand {odo} km. Je proefperiode duurt $trial_days "
                        "dagen.</p>{button}<p>Daarna stuur je na elke rit je kilometerstand en bestemming, "
                        "bijvoorbeeld <code>45230 Kantoor</code>.</p>"),
            "button": 'Open WhatsApp en stuur "start"',
            "failed": ("Niet gelukt", '<h1>De machtiging is niet gelukt</h1><p>Er is niets afgeschreven. '
                       '<a href="/#aanmelden">Probeer het opnieuw</a>.</p>'),
            "attention": ("Bijna klaar", "<h1>Betaling ontvangen</h1><p>We zetten je nummer handmatig aan "
                          "en sturen je binnen een werkdag een bericht.</p>"),
            "inactive": ("Aanmelding", "<h1>Deze aanmelding is niet meer actief.</h1>"),
        },
        "en": {
            "unknown": ("Sign up", "<h1>Unknown signup</h1><p>This link isn't right.</p>"),
            "back": ("Welcome back", "<h1>Welcome back, {first}!</h1><p>Your mandate is renewed and your "
                     "subscription is running again. Send your odometer reading and destination in "
                     "WhatsApp as usual.</p>"),
            "renew_failed": ("That didn't work", "<h1>The mandate didn't go through</h1><p>Nothing was "
                             "charged. Send 'resume' in WhatsApp for a new link.</p>"),
            "wait": ("One moment", "<h1>Processing your payment</h1><p>Mollie hasn't confirmed your payment "
                     "yet. This page refreshes itself.</p>"),
            "welcome": ("Welcome", "<h1>Welcome, {first}!</h1><p>Your number +{phone} is set up for "
                        "<strong>{car}</strong>, starting at {odo} km. Your free trial lasts $trial_days "
                        "days.</p>{button}<p>From now on, after every trip send your odometer reading and "
                        "destination, for example <code>45230 Office</code>. The bot answers in "
                        "English.</p>"),
            "button": 'Open WhatsApp and send "start"',
            "failed": ("That didn't work", '<h1>The mandate didn\'t go through</h1><p>Nothing was charged. '
                       '<a href="/en/#signup">Try again</a>.</p>'),
            "attention": ("Almost done", "<h1>Payment received</h1><p>We'll switch your number on by hand "
                          "and message you within one working day.</p>"),
            "inactive": ("Signup", "<h1>This signup is no longer active.</h1>"),
        },
    }

    @router.get("/aanmelden/klaar", response_class=HTMLResponse)
    async def done(id: str = "") -> HTMLResponse:
        try:
            signup = await billing.sync_signup(id)
        except MollieError:
            log.exception("Mollie sync on return failed")
            signup = billing.store.get(id)
        if signup is None:
            return page(*DONE["nl"]["unknown"], 404)
        lang = signup.lang if signup.lang in DONE else "nl"
        texts = DONE[lang]

        def show(key: str, **kw) -> HTMLResponse:
            title, body = texts[key]
            return page(title, body.format(**kw), lang=lang, head_extra=(
                '<meta http-equiv="refresh" content="5">' if key == "wait" else ""))

        first = html.escape(signup.name.split()[0]) if signup.name.split() else ""
        if signup.renew_payment_id and signup.status == ACTIVE:
            return show("back", first=first)
        if signup.renew_payment_id and signup.status in (PAUSED, CANCELED, ENDED):
            return show("renew_failed" if signup.renew_failed else "wait")
        if signup.status == ACTIVE:
            number = settings.whatsapp_public_number
            link = f"https://wa.me/{number}?text={quote('start')}" if number else ""
            button = f'<p><a class="btn" href="{link}">{texts["button"]}</a></p>' if link else ""
            return show("welcome", first=first, phone=signup.phone, car=html.escape(signup.car_label),
                        odo=signup.seed_odometer, button=button)
        if signup.status == PENDING:
            return show("wait")
        if signup.status == FAILED:
            return show("failed")
        if signup.status == ATTENTION:
            return show("attention")
        return show("inactive")

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


def install_not_found(app: FastAPI, router: APIRouter) -> None:
    """Unknown paths get the site's own 404 page (a real 404 status) instead of
    FastAPI's ``{"detail": "Not Found"}``. Other HTTP errors keep the default."""
    from fastapi.exception_handlers import http_exception_handler

    async def handler(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404 and request.method in ("GET", "HEAD"):
            return router.not_found(lang_of(request.url.path))
        return await http_exception_handler(request, exc)

    app.add_exception_handler(StarletteHTTPException, handler)
