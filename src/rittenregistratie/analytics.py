"""First-party, cookieless site statistics and the subscription funnel.

Two sources, nothing sent anywhere:

* **Page counts** (:class:`PageCounter`): per day, how often each public page
  was viewed, from which referring *domain*, with which ``utm_source`` /
  ``utm_campaign``, and how many signup forms were posted. Only these daily
  totals are stored (``data/analytics.json``): no IP address, no cookie, no
  visitor id, nothing that tells one visitor from another. Crawlers and link
  preview fetchers are counted apart. Days older than ``KEEP_DAYS`` are dropped.
* **Funnel and money** from ``data/signups.json`` (billing.py): signups started,
  first payments, trials that turned into a paid charge, pauses, cancellations,
  and the monthly recurring revenue of the subscriptions running now.

:func:`report` renders both as a short WhatsApp text. The owner gets it with the
admin command ``stats [days]``, and weekly from the timer in ``deploy/``:

    python -m rittenregistratie.analytics report [--days 7]
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlsplit

from .billing import (
    ACTIVE, ATTENTION, CANCELED, ENDED, FAILED, PAUSED, PENDING, Signup, SignupStore,
)
from . import wa_health
from .config import Settings

KEEP_DAYS = 400
_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|telegram|curl|wget|"
    r"python|httpx|go-http|headless|lighthouse|monitor|uptime", re.I)
_TAG_RE = re.compile(r"[^a-z0-9._-]+")


def _tag(value: str, limit: int = 40) -> str:
    """A campaign or source value as a short, harmless key."""
    return _TAG_RE.sub("-", (value or "").strip().lower())[:limit].strip("-")


def referrer_domain(referer: str, own_host: str = "") -> str:
    """The referring site's domain without ``www.``; empty for none or ourselves."""
    try:
        host = (urlsplit(referer).hostname or "").lower()
    except ValueError:
        return ""
    host = host[4:] if host.startswith("www.") else host
    own = (own_host or "").lower()
    own = own[4:] if own.startswith("www.") else own
    if not host or host == own or host in ("localhost", "127.0.0.1"):
        return ""
    return host[:60]


class PageCounter:
    """Daily totals in ``data/analytics.json``::

        {"2026-11-01": {"views": {"/": 12}, "referrers": {"google.com": 3},
                        "sources": {"linkedin": 2}, "campaigns": {"launch": 2},
                        "forms": 1, "bots": 5}}
    """

    def __init__(self, path: Path, own_host: str = ""):
        self.path = path
        self.own_host = own_host
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except ValueError:
            return {}

    def _save(self, data: Dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _update(self, fn, today: Optional[date] = None) -> None:
        day = (today or date.today()).isoformat()
        with self._lock:
            data = self._load()
            fn(data.setdefault(day, {}))
            cutoff = ((today or date.today()) - timedelta(days=KEEP_DAYS)).isoformat()
            for old in [d for d in data if d < cutoff]:
                del data[old]
            self._save(data)

    def view(self, path: str, *, user_agent: str = "", referer: str = "", query: str = "",
             today: Optional[date] = None) -> None:
        """Count one page view. Called for public HTML pages only."""
        if _BOT_RE.search(user_agent or "") or not user_agent:
            self._update(lambda d: d.__setitem__("bots", d.get("bots", 0) + 1), today)
            return
        ref = referrer_domain(referer, self.own_host)
        q = parse_qs(query or "")
        source = _tag((q.get("utm_source") or [""])[0])
        campaign = _tag((q.get("utm_campaign") or [""])[0])

        def bump(d):
            for key, value in (("views", path), ("referrers", ref), ("sources", source),
                               ("campaigns", campaign)):
                if value:
                    d.setdefault(key, {})[value] = d.get(key, {}).get(value, 0) + 1
        self._update(bump, today)

    def form(self, today: Optional[date] = None) -> None:
        """Count one signup form post (before validation)."""
        self._update(lambda d: d.__setitem__("forms", d.get("forms", 0) + 1), today)

    def totals(self, start: date, end: date) -> dict:
        """Summed counts for ``start``..``end`` inclusive."""
        out = {"views": Counter(), "referrers": Counter(), "sources": Counter(),
               "campaigns": Counter(), "forms": 0, "bots": 0}
        for day, d in self._load().items():
            if not (start.isoformat() <= day <= end.isoformat()):
                continue
            for key in ("views", "referrers", "sources", "campaigns"):
                out[key].update(d.get(key, {}))
            out["forms"] += d.get("forms", 0)
            out["bots"] += d.get("bots", 0)
        return out


# --- funnel ------------------------------------------------------------------

def _created(s: Signup) -> date:
    return datetime.fromisoformat(s.created_at).date()


def _event_dates(s: Signup, text: str) -> List[date]:
    """Dates of audit events (``"<iso ts> <text>"``) that contain ``text``."""
    out = []
    for e in s.events:
        ts, _, rest = e.partition(" ")
        if text in rest:
            try:
                out.append(datetime.fromisoformat(ts).date())
            except ValueError:
                pass
    return out


def _paid_first(s: Signup) -> bool:
    return bool(s.subscription_id or s.old_subscription_ids or s.status == ATTENTION)


def _monthly_value(s: Signup, settings: Settings) -> float:
    if s.plan == "yearly":
        return float(settings.price_yearly) / 12
    return float(settings.price_monthly)


def funnel(signups: Iterable[Signup], settings: Settings, start: date, end: date,
           today: Optional[date] = None) -> dict:
    today = today or date.today()
    signups = list(signups)
    period = [s for s in signups if start <= _created(s) <= end]
    running = [s for s in signups if s.status == ACTIVE]
    in_trial = [s for s in running if s.trial_until and date.fromisoformat(s.trial_until) >= today]
    paying = [s for s in running if s not in in_trial]
    trials_done = [s for s in signups if _paid_first(s) and s.trial_until
                   and start <= date.fromisoformat(s.trial_until) <= min(end, today)]
    converted = [s for s in trials_done
                 if any(" recurring " in e and e.endswith(" paid") for e in s.events)]
    return {
        "started": len(period),
        "paid_first": sum(1 for s in period if _paid_first(s)),
        "failed": sum(1 for s in period if s.status == FAILED),
        "pending": sum(1 for s in period if s.status == PENDING),
        "trials_ended": len(trials_done),
        "trials_converted": len(converted),
        "paused": sum(1 for s in signups if any(start <= d <= end for d in _event_dates(s, "paused by customer"))),
        "cancelled": sum(1 for s in signups if any(start <= d <= end for d in _event_dates(s, "cancelled"))),
        "active": len(running),
        "in_trial": len(in_trial),
        "paused_now": sum(1 for s in signups if s.status == PAUSED),
        "attention": sum(1 for s in signups if s.status == ATTENTION),
        "mrr_paying": round(sum(_monthly_value(s, settings) for s in paying), 2),
        "mrr_incl_trial": round(sum(_monthly_value(s, settings) for s in running), 2),
        "monthly": sum(1 for s in running if s.plan != "yearly"),
        "yearly": sum(1 for s in running if s.plan == "yearly"),
    }


# --- report ------------------------------------------------------------------

def _eur(v: float) -> str:
    return f"€{v:,.2f}"


def _top(c: Counter, n: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "none"


def report(settings: Settings, days: int = 7, today: Optional[date] = None) -> str:
    """The owner's summary. Always English, whatever language the owner chose."""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    host = urlsplit(settings.public_base_url).hostname or ""
    t = PageCounter(settings.analytics_file, host).totals(start, today)
    f = funnel(SignupStore(settings.signups_file).all(), settings, start, today, today)
    views = t["views"]
    home = views.get("/", 0)
    english = sum(v for p, v in views.items() if p == "/en" or p.startswith("/en/"))
    lines = [
        f"RittenRegistratie-bot, last {days} days ({start:%d %b} to {today:%d %b})",
        "",
        f"Site: {sum(views.values())} page views, {home} on the homepage"
        + (f" (+{t['bots']} bots)" if t["bots"] else ""),
        f"English site: {english} page views, {views.get('/en/', 0)} on its homepage",
        f"Referrers: {_top(t['referrers'])}",
    ]
    if t["sources"] or t["campaigns"]:
        lines.append(f"Campaigns: {_top(t['campaigns'] + t['sources'])}")
    rate = f" ({f['paid_first'] / home:.0%} of homepage views)" if home else ""
    lines += [
        "",
        f"Funnel: {t['forms']} forms -> {f['started']} to payment -> "
        f"{f['paid_first']} mandates paid{rate}",
    ]
    if f["failed"] or f["pending"]:
        lines.append(f"  not completed: {f['failed']} failed, {f['pending']} open")
    if f["trials_ended"]:
        lines.append(f"Trials ended: {f['trials_ended']}, {f['trials_converted']} converted to paid")
    if f["paused"] or f["cancelled"]:
        lines.append(f"Paused: {f['paused']}, cancelled: {f['cancelled']}")
    lines += [
        "",
        f"Now: {f['active']} active ({f['in_trial']} in trial, {f['monthly']} monthly, "
        f"{f['yearly']} yearly), {f['paused_now']} paused",
        f"MRR: {_eur(f['mrr_paying'])} paying, {_eur(f['mrr_incl_trial'])} incl. trials",
    ]
    if f["attention"]:
        lines.append(f"Attention: {f['attention']} paid signup(s) waiting for you.")
    lines += ["", _invoices_line(settings, start, today), wa_health.report_line(settings)]
    return "\n".join(lines)


def _invoices_line(settings: Settings, start: date, end: date) -> str:
    from .invoices import InvoiceStore
    mine = [i for i in InvoiceStore(settings.invoices_dir).all()
            if not i.test and start <= date.fromisoformat(i.issued) <= end]
    return (f"Invoices: {len(mine)} issued, {_eur(sum(float(i.total) for i in mine))} incl. VAT"
            f" ({_eur(sum(float(i.vat) for i in mine))} VAT)")


def main(argv: Optional[List[str]] = None) -> int:
    from .config import get_settings
    ap = argparse.ArgumentParser(description="Site statistics and subscription funnel.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="print the summary")
    r.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    print(report(get_settings(), days=args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
