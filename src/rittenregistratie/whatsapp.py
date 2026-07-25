"""WhatsApp Cloud API helpers: signature verification, parsing, sending."""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import httpx

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
    """Return {from, text} for the first text message, or None."""
    try:
        change = body["entry"][0]["changes"][0]["value"]
        messages = change.get("messages")
        if not messages:
            return None
        msg = messages[0]
        if msg.get("type") != "text":
            return {"from": msg.get("from"), "text": None}
        return {"from": msg["from"], "text": msg["text"]["body"]}
    except (KeyError, IndexError, TypeError):
        return None


async def send_message(
    token: str, phone_number_id: str, to: str, text: str
) -> None:
    if not token or not phone_number_id:
        return
    url = f"{GRAPH_URL}/{phone_number_id}/messages"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
        )
