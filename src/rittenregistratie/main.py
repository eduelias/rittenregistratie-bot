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


@app.get("/selftest")
async def selftest(to: str = "") -> dict:
    """Send a test WhatsApp message and report the raw Graph API result.

    Use ?to=<E.164 without +> to override the recipient; defaults to the
    configured allowed sender. Helps confirm whether a recipient is allow-listed
    on a Meta test number (success vs error 131037) without logging any trip.
    """
    import httpx

    recipient = to or _settings.allowed_sender
    if not recipient:
        return {"ok": False, "error": "no recipient; pass ?to=<number>"}
    if not (_settings.whatsapp_token and _settings.whatsapp_phone_number_id):
        return {"ok": False, "error": "whatsapp token/phone id not configured"}
    url = (
        f"https://graph.facebook.com/{_settings.whatsapp_graph_version}/"
        f"{_settings.whatsapp_phone_number_id}/messages"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {_settings.whatsapp_token}"},
            json={
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": "selftest: bot -> you. If you see this, replies work."},
            },
        )
    return {"ok": resp.status_code < 400, "status": resp.status_code, "body": resp.json()}


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

    # Log delivery/read/failed status receipts (when subscribed to 'statuses').
    for st in whatsapp.extract_statuses(body):
        if st.get("error"):
            log.warning(
                "Message %s to %s: %s (%s)",
                st.get("id"), st.get("recipient"), st.get("status"), st["error"],
            )
        else:
            log.info(
                "Message %s to %s: %s",
                st.get("id"), st.get("recipient"), st.get("status"),
            )

    msg = whatsapp.extract_message(body)
    # Always 200 quickly so Meta does not retry.
    if not msg or not msg.get("from"):
        return Response(status_code=200, content="ok")

    sender = msg["from"]
    location = msg.get("location")
    text = msg.get("text")

    # Extra reply recipients (e.g. notify admins of a new join request).
    extra_notify: list[tuple[str, str]] = []

    is_registered = bool(_engine.cars.resolve(sender))
    is_admin = _engine.is_admin(sender)

    # Admin onboarding commands (approve/deny/pending/help).
    from .onboarding import parse_admin_command
    admin_cmd, admin_args = parse_admin_command(text or "")

    if is_admin and admin_cmd:
        try:
            reply = _engine.handle_admin_command(admin_cmd, admin_args)
            # If a user was just approved, welcome them too.
            if admin_cmd == "approve" and reply.startswith("Approved"):
                approved = _engine.cars.resolve(admin_args[0]) if admin_args else None
                if approved:
                    extra_notify.append((
                        _engine.cars.resolve(admin_args[0]).phones[0],
                        "You're approved! Send '<odometer> <destination>' to log a "
                        "trip, or 'ping' to test. First message sets your car's start.",
                    ))
        except Exception:  # pragma: no cover
            log.exception("admin command failed")
            reply = "Admin command failed."
    elif not is_registered:
        # Onboarding: record a join request and notify admins.
        if not _settings.onboarding_enabled:
            log.warning("Ignoring message from unregistered number %s", sender)
            return Response(status_code=200, content="ok")
        if _engine.onboarding.has(sender):
            reply = "Your request to join is still pending admin approval. Hang tight!"
        else:
            _engine.register_join_request(sender, text or "(non-text message)")
            reply = (
                "Hi! This is RittenRegistratie-Bot. Your number isn't registered "
                "yet. I've sent a join request to the admin — you'll get a message "
                "when you're approved."
            )
            for admin in _settings.admin_list():
                extra_notify.append((
                    admin,
                    f"New join request from {sender}: \"{(text or '')[:60]}\".\n"
                    f"Approve: approve {sender} <label> <seed_odo> [address]\n"
                    f"Or: deny {sender}",
                ))
        # send onboarding reply + notifications below
        await _send_all(sender, reply, extra_notify)
        return Response(status_code=200, content="ok")
    else:
        try:
            if text and text.strip().lower() in ("ping", "/ping"):
                # Simple connectivity check: no trip logged, no state touched.
                reply = "pong"
            elif location:
                reply = _engine.handle_location(
                    sender,
                    location.get("latitude"),
                    location.get("longitude"),
                    location.get("address") or "",
                )
            elif text:
                reply = _engine.handle_text(text, sender)
            else:
                reply = (
                    "Please send: <odometer> <destination> [private], "
                    "or share a location when asked."
                )
        except (ParseError, EngineError, UnknownCarError) as exc:
            reply = f"Could not log trip: {exc}"
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected error handling message")
            reply = "Internal error while logging the trip."

    await _send_all(sender, reply, extra_notify)
    return Response(status_code=200, content="ok")


async def _send_all(sender: str, reply: str, extra: list) -> None:
    """Send the main reply plus any extra notifications."""
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    delivered = await whatsapp.send_message(
        _settings.whatsapp_token, _settings.whatsapp_phone_number_id,
        sender, reply, graph_url=graph_url,
    )
    if not delivered:
        # Privacy: do not write message/reply content to the journal.
        log.warning("Reply not delivered to %s (see WhatsApp error above).", sender)
    for to, body in extra:
        await whatsapp.send_message(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id,
            to, body, graph_url=graph_url,
        )
