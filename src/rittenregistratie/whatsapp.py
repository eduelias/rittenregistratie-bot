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


def verify_signature(app_secret: str, payload: bytes, header: str,
                     allow_unsigned: bool = False) -> bool:
    """Verify the X-Hub-Signature-256 header from Meta.

    Without an app secret nothing can be verified, and every request is
    refused: a mistyped variable name must not open the admin commands to
    anyone who can post JSON. ``allow_unsigned`` is for local development."""
    if not app_secret:
        return allow_unsigned
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    # bytes: compare_digest raises on a non-ASCII str, which would be a 500
    return hmac.compare_digest(expected.encode(), header.split("=", 1)[1].encode("utf-8"))


def _values(body: dict) -> list:
    """Every ``entry[].changes[].value`` of a webhook payload. Meta may batch
    several entries, changes and messages into one delivery."""
    out = []
    try:
        for entry in body.get("entry") or []:
            for change in entry.get("changes") or []:
                if isinstance(change.get("value"), dict):
                    out.append(change["value"])
    except (AttributeError, TypeError):
        pass
    return out


# Not something the driver wrote to the bot: answering these with the usage
# text sent an instruction after every thumbs-up.
_NOT_MESSAGES = frozenset({"reaction", "unsupported", "system"})


def extract_messages(body: dict) -> list:
    """Every inbound message of a payload, in order.

    Each is a dict with ``from``, ``id`` (the WhatsApp message id, used to
    react to the message) and one of ``text`` (str) or ``location``
    ({latitude, longitude, address?, name?}). For other types (image, audio,
    button, ...) ``text`` is None. Reactions and system notices are left out.
    """
    out = []
    for value in _values(body):
        for msg in value.get("messages") or []:
            try:
                mtype = msg.get("type")
                if mtype in _NOT_MESSAGES:
                    continue
                item = {"from": msg.get("from"), "id": msg.get("id"), "text": None}
                if mtype == "text":
                    item["text"] = msg["text"]["body"]
                elif mtype == "location":
                    loc = msg.get("location", {})
                    item["location"] = {
                        "latitude": loc.get("latitude"),
                        "longitude": loc.get("longitude"),
                        "address": loc.get("address"),
                        "name": loc.get("name"),
                    }
                out.append(item)
            except (KeyError, AttributeError, TypeError):
                continue
    return out


def extract_waba_id(body: dict) -> Optional[str]:
    """The WhatsApp Business Account id a webhook came from (``entry[].id``).

    Not derivable from a system-user token through the API, and needed to
    manage templates - so the first webhook is the easiest place to learn it.
    """
    try:
        return str(body["entry"][0]["id"]) or None
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
        for change in _values(body):
            for s in change.get("statuses", []) or []:
                errs = s.get("errors") or []
                out.append({
                    "id": s.get("id"),
                    "status": s.get("status"),
                    "recipient": s.get("recipient_id"),
                    "error": (errs[0].get("title") if errs else None),
                    "code": (errs[0].get("code") if errs else None),
                })
    except (KeyError, IndexError, TypeError, AttributeError):
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


async def send_template(
    token: str, phone_number_id: str, to: str, name: str, params: list,
    language: str = "en", graph_url: str = GRAPH_URL,
) -> bool:
    """Send an approved message template with positional body parameters.

    A free-form text can only be delivered within 24 hours of the recipient's
    last message (Meta error 131047, "Re-engagement message", outside it). A
    template has no such window, which is what a notification the recipient
    did not prompt - a trip logged by the car - needs.
    """
    return await _post_message(
        token, phone_number_id,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)} for p in params],
                }],
            },
        },
        graph_url, f"template {name}",
    )


async def send_auth_code(
    token: str, phone_number_id: str, to: str, name: str, code: str,
    language: str = "en", graph_url: str = GRAPH_URL,
) -> bool:
    """Send a one-time code with an AUTHENTICATION template (copy-code button).

    Such a template is delivered whatever the 24-hour window; Meta fixes its
    wording ("<code> is your verification code") in each language.
    """
    return await _post_message(
        token, phone_number_id,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": code}]},
                    {"type": "button", "sub_type": "url", "index": "0",
                     "parameters": [{"type": "text", "text": code}]},
                ],
            },
        },
        graph_url, f"auth template {name}",
    )


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def upload_media(
    token: str, phone_number_id: str, path, mime: str = XLSX_MIME,
    graph_url: str = GRAPH_URL,
) -> Optional[str]:
    """Upload a file to the Cloud API media endpoint. Returns the media id.

    Returns None (and logs) on failure. The id is then used by
    :func:`send_document`.
    """
    from pathlib import Path as _Path
    if not token or not phone_number_id:
        return None
    path = _Path(path)
    url = f"{graph_url}/{phone_number_id}/media"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with path.open("rb") as fh:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (path.name, fh, mime)},
                )
        if resp.status_code >= 400:
            log.warning("WhatsApp media upload failed (%s): %s", resp.status_code, resp.text)
            return None
        return resp.json().get("id") or None
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("WhatsApp media upload errored: %s", exc)
        return None


async def send_document(
    token: str, phone_number_id: str, to: str, media_id: str, filename: str,
    caption: str = "", graph_url: str = GRAPH_URL,
) -> bool:
    """Send a previously uploaded file as a document message."""
    if not media_id:
        return False
    document = {"id": media_id, "filename": filename}
    if caption:
        document["caption"] = caption
    return await _post_message(
        token, phone_number_id,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "document",
            "document": document,
        },
        graph_url, "document",
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
