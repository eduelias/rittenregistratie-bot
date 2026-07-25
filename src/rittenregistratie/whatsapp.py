"""WhatsApp Cloud API helpers: signature verification, parsing, sending."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

import httpx

log = logging.getLogger("rittenregistratie.whatsapp")

GRAPH_URL = "https://graph.facebook.com/v21.0"


def verify_signature(app_secret: str, payload: bytes, header: str) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta."""
    if not app_secret:
        return True  # verification disabled (e.g. local dev)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def extract_message(body: dict) -> Optional[dict]:
    """Return a dict describing the first message.

    Keys: ``from``; and one of ``text`` (str) or ``location`` ({latitude,
    longitude, address?, name?}). For non-text/non-location types, ``text`` is
    None.
    """
    try:
        change = body["entry"][0]["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None
        msg = messages[0]
        sender = msg.get("from")
        mtype = msg.get("type")
        if mtype == "text":
            return {"from": sender, "text": msg["text"]["body"]}
        if mtype == "location":
            loc = msg.get("location", {})
            return {
                "from": sender,
                "text": None,
                "location": {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "address": loc.get("address"),
                    "name": loc.get("name"),
                },
            }
        return {"from": sender, "text": None}
    except (KeyError, IndexError, TypeError):
        return None


def reverse_geocode(lat, lon, api_key: str = "") -> str:
    """Reverse-geocode a lat/lon into a human address via Google (best effort).

    Returns '' if no key is set or the lookup fails, so callers can fall back to
    storing the raw coordinates.
    """
    if not api_key or lat is None or lon is None:
        return ""
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": api_key},
            timeout=10.0,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            return results[0].get("formatted_address", "")
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    return ""


async def send_message(
    token: str, phone_number_id: str, to: str, text: str
) -> bool:
    """Send a WhatsApp text reply. Returns True on success, False otherwise.

    Delivery failures (e.g. Meta error 131037 display-name approval, or the
    recipient not being on a test number's allow-list) are logged so they are
    visible in the service journal instead of failing silently.
    """
    if not token or not phone_number_id:
        return False
    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )
        if resp.status_code >= 400:
            log.warning(
                "WhatsApp reply to %s failed (%s): %s",
                to, resp.status_code, resp.text,
            )
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("WhatsApp reply to %s errored: %s", to, exc)
        return False
