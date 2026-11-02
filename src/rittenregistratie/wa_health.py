"""WhatsApp number health: Meta's quality rating and messaging limit.

Meta rates the bot's number GREEN, YELLOW or RED from how recipients react
(blocks, reports), and caps how many people the business may start a
conversation with per 24 hours (the messaging limit: TIER_250, TIER_2K, ...,
since 2025 set per business portfolio). A RED rating or a lower limit means
templates (trip notices, invoices, alerts) start failing.

The app reads both every few hours (:func:`watch`), keeps the last reading in
``data/whatsapp-health.json`` and alerts the admins through the
``subscriber_event`` template when the rating drops or the limit changes. The
weekly report shows the last reading (:func:`report_line`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

import httpx

from .config import Settings

log = logging.getLogger("rittenregistratie.wa_health")

FIELDS = ("quality_rating,messaging_limit_tier,whatsapp_business_manager_messaging_limit,"
          "display_phone_number")
_RANK = {"GREEN": 3, "YELLOW": 2, "RED": 1}
INTERVAL_S = 6 * 3600

# alert(key, **kwargs): a message to every admin (main.py renders and sends it).
Alert = Callable[..., Awaitable[object]]


async def fetch(settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    url = (f"https://graph.facebook.com/{settings.whatsapp_graph_version}/"
           f"{settings.whatsapp_phone_number_id}")
    async with httpx.AsyncClient(timeout=20.0, transport=transport) as client:
        resp = await client.get(url, params={"fields": FIELDS},
                                headers={"Authorization": f"Bearer {settings.whatsapp_token}"})
    resp.raise_for_status()
    data = resp.json()
    return {
        "quality": data.get("quality_rating") or "UNKNOWN",
        # messaging_limit_tier is the old per-number field; Meta now reports
        # the portfolio's limit instead.
        "tier": (data.get("whatsapp_business_manager_messaging_limit")
                 or data.get("messaging_limit_tier") or "UNKNOWN"),
        "number": data.get("display_phone_number") or "",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def load(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(path: Path, reading: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reading, indent=2), encoding="utf-8")
    tmp.replace(path)


def changes(old: Optional[dict], new: dict) -> List[tuple]:
    """(alert key, kwargs) for a rating drop or a limit change."""
    if not old:
        return []
    out = []
    if _RANK.get(new["quality"], 0) < _RANK.get(old.get("quality"), 0):
        out.append(("alert.wa_quality", {"old": old["quality"], "new": new["quality"]}))
    if new["tier"] != old.get("tier") and "UNKNOWN" not in (new["tier"], old.get("tier")):
        out.append(("alert.wa_tier", {"old": old.get("tier"), "new": new["tier"]}))
    return out


async def check(settings: Settings, alert: Alert,
                transport: Optional[httpx.AsyncBaseTransport] = None) -> Optional[dict]:
    """Read the number's health, store it and alert on a change."""
    try:
        new = await fetch(settings, transport)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("WhatsApp health check failed: %s", exc)
        return None
    path = settings.whatsapp_health_file
    old = load(path)
    save(path, new)
    for key, kw in changes(old, new):
        log.warning("WhatsApp health: %s %s", key, kw)
        await alert(key, number=new["number"], **kw)
    return new


async def watch(settings: Settings, alert: Alert) -> None:  # pragma: no cover - a loop
    while True:
        await check(settings, alert)
        await asyncio.sleep(INTERVAL_S)


def report_line(settings: Settings) -> str:
    """For the owner's weekly report (English)."""
    r = load(settings.whatsapp_health_file)
    if not r:
        return "WhatsApp: not checked yet"
    when = datetime.fromisoformat(r["checked_at"])
    return f"WhatsApp: quality {r['quality']}, messaging limit {r['tier']} (checked {when:%d %b %H:%M})"
