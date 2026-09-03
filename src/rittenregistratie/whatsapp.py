"""WhatsApp Cloud API helpers: signature verification, parsing, sending."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Optional

import httpx

log = logging.getLogger("rittenregistratie.whatsapp")

# Default Meta Graph API version. Callers may override via graph_url/version.
GRAPH_VERSION = "v25.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"


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

    Keys: ``from``, ``id`` (the WhatsApp message id, used to react to the
    message); and one of ``text`` (str) or ``location`` ({latitude, longitude,
    address?, name?}). For non-text/non-location types, ``text`` is None.
    """
    try:
        change = body["entry"][0]["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None
        msg = messages[0]
        sender = msg.get("from")
        msg_id = msg.get("id")
        mtype = msg.get("type")
        if mtype == "text":
            return {"from": sender, "id": msg_id, "text": msg["text"]["body"]}
        if mtype == "location":
            loc = msg.get("location", {})
            return {
                "from": sender,
                "id": msg_id,
                "text": None,
                "location": {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "address": loc.get("address"),
                    "name": loc.get("name"),
                },
            }
        return {"from": sender, "id": msg_id, "text": None}
    except (KeyError, IndexError, TypeError):
        return None


def extract_statuses(body: dict) -> list:
    """Return delivery/read/failed status events from a webhook payload.

    Present when the WABA is subscribed to the ``statuses`` field. Returns a list
    of dicts with ``id``, ``status`` (sent/delivered/read/failed), ``recipient``
    and optional ``error``. Empty list when there are no statuses.
    """
    out = []
    try:
        change = body["entry"][0]["changes"][0]["value"]
        for s in change.get("statuses", []) or []:
            errs = s.get("errors") or []
            out.append({
                "id": s.get("id"),
                "status": s.get("status"),
                "recipient": s.get("recipient_id"),
                "error": (errs[0].get("title") if errs else None),
            })
    except (KeyError, IndexError, TypeError):
        pass
    return out


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


async def _post_message(
    token: str, phone_number_id: str, payload: dict, graph_url: str, what: str,
) -> bool:
    """POST one message payload to the Cloud API. Returns True on success.

    Delivery failures (e.g. Meta error 131037 display-name approval, or the
    recipient not being on a test number's allow-list) are logged so they are
    visible in the service journal instead of failing silently.
    """
    if not token or not phone_number_id:
        return False
    to = payload.get("to", "?")
    url = f"{graph_url}/{phone_number_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}, json=payload,
            )
        if resp.status_code >= 400:
            body = resp.text
            log.warning(
                "WhatsApp %s to %s failed (%s): %s", what, to, resp.status_code, body,
            )
            if "131037" in body or "131047" in body:
                log.warning(
                    "HINT: with a Meta Public Test Number, the recipient %s must "
                    "be added and verified in API Setup -> 'To' before replies can "
                    "be delivered. This does not affect trip logging.",
                    to,
                )
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("WhatsApp %s to %s errored: %s", what, to, exc)
        return False


async def send_message(
    token: str, phone_number_id: str, to: str, text: str,
    graph_url: str = GRAPH_URL,
) -> bool:
    """Send a WhatsApp text reply. Returns True on success, False otherwise."""
    return await _post_message(
        token, phone_number_id,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
        graph_url, "reply",
    )


async def send_reaction(
    token: str, phone_number_id: str, to: str, message_id: str,
    emoji: str = "\U0001F44D", graph_url: str = GRAPH_URL,
) -> bool:
    """React to the user's message (default thumbs-up). Returns True on success.

    ``message_id`` is the ``id`` of the inbound message from the webhook. Meta
    only delivers reactions to messages under 30 days old and emits just a
    'sent' status for them. Pass ``emoji=""`` to remove a reaction.
    """
    if not message_id:
        return False
    return await _post_message(
        token, phone_number_id,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "reaction",
            "reaction": {"message_id": message_id, "emoji": emoji},
        },
        graph_url, "reaction",
    )
