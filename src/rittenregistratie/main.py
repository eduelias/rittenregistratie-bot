"""FastAPI application: WhatsApp Cloud API webhook."""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request, Response
from starlette.background import BackgroundTask

from . import whatsapp
from .config import get_settings
from .engine import Engine, EngineError, ExportRequest, UnknownCarError
from .export import ExportError
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
    message_id = msg.get("id")
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
        # Spreadsheet request: acknowledge now, generate and send in the
        # background (a plugin may take a while; Meta wants a fast 200).
        try:
            export_req = _engine.parse_export_command(text or "", sender)
        except (EngineError, UnknownCarError) as exc:
            await _send_all(sender, f"Could not export: {exc}", extra_notify)
            return Response(status_code=200, content="ok")
        if export_req is not None:
            await _ack_export(sender, message_id)
            return Response(
                status_code=200, content="ok",
                background=BackgroundTask(_run_export, sender, export_req),
            )
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

    await _send_all(sender, reply, extra_notify, message_id=message_id)
    return Response(status_code=200, content="ok")


async def _send_all(
    sender: str, reply: str, extra: list, message_id: str | None = None,
) -> None:
    """Acknowledge the sender, then send any extra notifications.

    RIT_REPLY_MODE decides the acknowledgement for a logged trip:
    - ``text``: the summary message (default).
    - ``reaction``: a thumbs-up on the sender's own message; text is sent only
      when the bot needs something (address prompt, error) or has extra
      information (learned address, route deviation, cap warning).
    - ``both``: reaction and text.
    If the reaction cannot be sent, the text is sent instead so every message
    gets an acknowledgement.
    """
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    mode = (_settings.reply_mode or "text").strip().lower()
    logged = bool(getattr(reply, "logged", False))
    notice = bool(getattr(reply, "notice", False))

    react = mode in ("reaction", "both") and logged and bool(message_id)
    send_text = mode != "reaction" or not react or notice
    if react:
        reacted = await whatsapp.send_reaction(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id,
            sender, message_id, graph_url=graph_url,
        )
        if not reacted:
            log.warning("Reaction not delivered to %s; sending text instead.", sender)
            send_text = True
    if send_text:
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


async def _ack_export(sender: str, message_id: str | None) -> None:
    """Tell the sender the export started: an hourglass reaction, or text."""
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    mode = (_settings.reply_mode or "text").strip().lower()
    if mode in ("reaction", "both") and message_id:
        ok = await whatsapp.send_reaction(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id,
            sender, message_id, emoji="\u23F3", graph_url=graph_url,
        )
        if ok and mode == "reaction":
            return
    await whatsapp.send_message(
        _settings.whatsapp_token, _settings.whatsapp_phone_number_id,
        sender, "Generating your spreadsheet\u2026", graph_url=graph_url,
    )


async def _run_export(sender: str, req: ExportRequest) -> None:
    """Generate one workbook per requested year and send each as a document."""
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    token, pid = _settings.whatsapp_token, _settings.whatsapp_phone_number_id

    async def tell(text: str) -> None:
        await whatsapp.send_message(token, pid, sender, text, graph_url=graph_url)

    for year in req.years:
        try:
            result = await asyncio.to_thread(_engine.exporter.generate, req.car.car_id, year)
        except ExportError as exc:
            await tell(f"Export {year} failed: {exc}")
            continue
        except Exception:  # pragma: no cover - defensive
            log.exception("export failed for %s %s", req.car.car_id, year)
            await tell(f"Export {year} failed: internal error.")
            continue
        media_id = await whatsapp.upload_media(token, pid, result.path, graph_url=graph_url)
        if not media_id:
            await tell(f"Export {year} is ready but the upload to WhatsApp failed.")
            continue
        caption = f"{req.car.label} \u2014 {year} \u2014 {result.rows} trips"
        if result.handlers:
            caption += f" (processed by {result.handlers} plugin"
            caption += "s)" if result.handlers != 1 else ")"
        sent = await whatsapp.send_document(
            token, pid, sender, media_id, result.path.name, caption=caption,
            graph_url=graph_url,
        )
        if not sent:
            await tell(f"Export {year} is ready but could not be sent.")
        else:
            log.info("Export %s/%s sent to %s (%s rows).", req.car.car_id, year, sender, result.rows)
