"""FastAPI application: WhatsApp Cloud API webhook."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response

from . import whatsapp
from .config import get_settings
from .engine import Engine, EngineError, UnknownCarError
from .parser import ParseError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rittenregistratie")

app = FastAPI(title="rittenregistratie-bot")
_settings = get_settings()
_engine = Engine(_settings)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/webhook")
async def verify(request: Request) -> Response:
    """Meta webhook verification handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    if mode == "subscribe" and token == _settings.whatsapp_verify_token:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403, content="Verification failed")


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not whatsapp.verify_signature(_settings.whatsapp_app_secret, raw, sig):
        return Response(status_code=403, content="Bad signature")

    body = await request.json()
    msg = whatsapp.extract_message(body)
    # Always 200 quickly so Meta does not retry.
    if not msg or not msg.get("from"):
        return Response(status_code=200, content="ok")

    sender = msg["from"]
    # Authorisation is driven by the car registry: only numbers registered to a
    # car are accepted. An optional extra allow-list can further restrict.
    if _settings.allowed_sender and sender != _settings.allowed_sender:
        log.warning("Ignoring message from non-allowed sender %s", sender)
        return Response(status_code=200, content="ok")
    if not _engine.cars.resolve(sender):
        log.warning("Ignoring message from unregistered number %s", sender)
        return Response(status_code=200, content="ok")

    text = msg.get("text")
    if not text:
        reply = "Please send a text message: <odometer> <destination> [private]"
    else:
        try:
            reply = _engine.handle_text(text, sender)
        except (ParseError, EngineError, UnknownCarError) as exc:
            reply = f"Could not log trip: {exc}"
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected error handling message")
            reply = "Internal error while logging the trip."

    await whatsapp.send_message(
        _settings.whatsapp_token,
        _settings.whatsapp_phone_number_id,
        sender,
        reply,
    )
    return Response(status_code=200, content="ok")
