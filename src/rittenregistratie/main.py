"""FastAPI application: WhatsApp Cloud API webhook."""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from . import whatsapp
from .config import get_settings
from .engine import Engine, EngineError, ExportRequest, UnknownCarError
from .billing import Billing, Mollie, MollieError
from .events import hook_event
from .export import ExportError
from .i18n import parse_language_command, set_current, t
from .window import InboundWindow
from .models import VehicleTripReport
from .parser import ParseError
from .site import build_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rittenregistratie")

app = FastAPI(title="rittenregistratie-bot")
_settings = get_settings()
_engine = Engine(_settings)


async def _notify(phone: str, text: str) -> bool:
    return await whatsapp.send_message(
        _settings.whatsapp_token, _settings.whatsapp_phone_number_id, phone, text,
        graph_url=f"https://graph.facebook.com/{_settings.whatsapp_graph_version}",
    )


_window = InboundWindow(_settings.last_inbound_file)


async def _alert(phone: str, text: str) -> bool:
    """A message to an admin. Plain text while their 24-hour window is open;
    else the alert template (one line, in their language, English as the
    fallback), since a plain text would be accepted and then dropped."""
    name = (_settings.whatsapp_alert_template or "").strip()
    if not name or _window.is_open(phone):
        return await _notify(phone, text)
    line = re.sub(r"\s{2,}", " ", " · ".join(
        part.strip() for part in str(text).splitlines() if part.strip()))[:900]
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    for code in dict.fromkeys((_engine.languages.of(phone), "en")):
        if await whatsapp.send_template(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id, phone, name, [line],
            language=code, graph_url=graph_url,
        ):
            return True
    log.warning("Alert template %r refused for %s; sending plain text.", name, phone)
    return await _notify(phone, text)


async def _send_code(phone: str, code: str) -> bool:
    """A one-time code for the website's cancel button: the authentication
    template, which reaches the number whatever its 24-hour window."""
    name = (_settings.whatsapp_code_template or "").strip()
    if not name:
        return False
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    for lang in dict.fromkeys((_engine.languages.of(phone), "en")):
        if await whatsapp.send_auth_code(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id, phone, name, code,
            language=lang, graph_url=graph_url,
        ):
            return True
    return False


async def _confirm_cancel(phone: str, reply: str, until: str) -> bool:
    """Confirm a cancellation made on the website on WhatsApp: the bot's own
    reply while the window is open, else the cancellation template."""
    name = (_settings.whatsapp_cancel_template or "").strip()
    if _window.is_open(phone) or not name or not until:
        return await _notify(phone, reply)
    from .i18n import date_text
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    for lang in dict.fromkeys((_engine.languages.of(phone), "en")):
        if await whatsapp.send_template(
            _settings.whatsapp_token, _settings.whatsapp_phone_number_id, phone, name,
            [date_text(until, lang)], language=lang, graph_url=graph_url,
        ):
            return True
    return await _notify(phone, reply)


_billing = (
    Billing(_settings, _engine, Mollie(_settings.mollie_api_key, _settings.mollie_api_url),
            _notify, alert=_alert)
    if _settings.billing_enabled else None
)
app.include_router(build_router(_settings, _billing, send_code=_send_code,
                                confirm_cancel=_confirm_cancel))


async def _subscription_reply(sender: str, text: str) -> str | None:
    """A subscription command (``abonnement``, ``opzeggen``, ``pauzeren``,
    ``hervatten``, a menu number, ...; see billing.COMMANDS), else None."""
    try:
        return await _billing.handle(sender, text)
    except MollieError:
        log.exception("subscription command failed")
        return t("sub.failed")


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


@app.post("/hooks/{plugin}/{car_id}")
async def vehicle_hook(plugin: str, car_id: str, request: Request) -> Response:
    """Inbound vehicle-telemetry post for one car, handled by one plugin.

    The plugin (enabled for the car via ``event_plugins``) turns the body into
    a VehicleTripReport; the core logs it like a typed message and notifies
    the car's numbers. Protected by ``X-Hook-Secret`` == RIT_HOOK_SECRET.
    """
    secret = _settings.hook_secret
    if not secret or request.headers.get("X-Hook-Secret", "") != secret:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    car = _engine.cars.get(car_id)
    if car is None:
        return JSONResponse({"error": f"unknown car '{car_id}'"}, status_code=404)
    event = hook_event(plugin)
    bus = _engine.bus_for(car_id)
    if not bus.handlers(event):
        return JSONResponse(
            {"error": f"plugin '{plugin}' is not enabled for car '{car_id}'"}, status_code=404,
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)
    payload = {
        "car_id": car_id, "plugin": plugin, "body": body,
        "headers": {k: v for k, v in request.headers.items() if k.lower().startswith("x-")},
    }
    try:
        result = bus.emit(event, payload)
    except Exception as exc:  # a plugin bug must not take the webhook down
        log.exception("hook plugin %s failed", plugin)
        return JSONResponse({"error": f"plugin error: {exc}"}, status_code=422)
    if not isinstance(result, VehicleTripReport):
        return JSONResponse({"status": "ignored", "reason": "plugin returned no trip"})
    result.source = result.source or plugin
    try:
        reply = _engine.handle_vehicle_trip(car, result)
    except (EngineError, UnknownCarError) as exc:
        return JSONResponse({"status": "rejected", "reason": str(exc)}, status_code=422)
    if reply is None:
        return JSONResponse({"status": "ignored", "reason": "reading not above the last logged one"})
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    notified = []
    for phone in car.phones:
        lang = _engine.languages.of(phone)
        set_current(lang)
        ok = await _notify_trip(phone, reply.in_lang(lang), graph_url, lang)
        notified.append({"to": phone, "delivered": ok})
    log.info("Vehicle trip logged for %s via %s (odo %s).", car_id, plugin, result.end_odo)
    return JSONResponse({"status": "logged", "end_odo": result.end_odo, "notified": notified})


async def _notify_trip(phone: str, reply, graph_url: str, lang: str = "") -> bool:
    """Tell the driver about a trip the car logged.

    Nobody texted the bot first, so a plain message is only deliverable inside
    the 24-hour window since the driver's last message - and with the car
    doing the logging, that window rarely reopens. When a template is
    configured it is used; if the send is refused (not yet approved, wrong
    parameter count) the plain text goes out instead, so a broken template
    degrades to today's behaviour rather than to silence.
    """
    token, pid = _settings.whatsapp_token, _settings.whatsapp_phone_number_id
    name = (_settings.whatsapp_trip_template or "").strip()
    facts = getattr(reply, "facts", None)
    if name and facts:
        # The template in the driver's language if Meta has that translation,
        # else in the configured one; the purpose word follows the template.
        tried = []
        for code in dict.fromkeys(c for c in (lang, _settings.whatsapp_trip_template_language) if c):
            params = [facts["label"], facts["origin"], facts["destination"], facts["km"],
                      t(f"purpose.{facts['purpose']}", "en" if code.startswith("en") else "nl"),
                      facts["odometer"]]
            if await whatsapp.send_template(token, pid, phone, name, params,
                                            language=code, graph_url=graph_url):
                return True
            tried.append(code)
        log.warning("Template %r (%s) was refused for %s; sending plain text instead.",
                    name, "/".join(tried), phone)
    return await whatsapp.send_message(token, pid, phone, reply, graph_url=graph_url)


_waba_logged = False


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

    global _waba_logged
    if not _waba_logged:
        waba = whatsapp.extract_waba_id(body)
        if waba:
            log.info("Webhook is for WhatsApp Business Account %s.", waba)
            _waba_logged = True

    # Log delivery/read/failed status receipts (when subscribed to 'statuses').
    for st in whatsapp.extract_statuses(body):
        if st.get("error"):
            log.warning(
                "Message %s to %s: %s (%s)",
                st.get("id"), st.get("recipient"), st.get("status"), st["error"],
            )
            if st.get("code") == 131047:
                log.warning(
                    "HINT: 131047 means the recipient has not messaged the bot in "
                    "24 hours, so a plain text cannot be delivered. Trips logged by "
                    "the car need RIT_WHATSAPP_TRIP_TEMPLATE set to an approved "
                    "template; the trip itself was recorded."
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
    # The sender's 24-hour window is open again (window.py).
    _window.seen(sender)
    # Everything below answers in the sender's language (i18n.py).
    set_current(_engine.languages.of(sender))

    # 'taal' / 'language' / 'english' / 'nederlands': works for every number.
    is_lang_cmd, chosen = parse_language_command(text or "")
    if is_lang_cmd:
        if chosen:
            _engine.languages.set(sender, chosen)
            set_current(chosen)
        await _send_all(sender, t("lang.set" if chosen else "lang.show"), [])
        return Response(status_code=200, content="ok")

    # Extra reply recipients (e.g. notify admins of a new join request).
    extra_notify: list[tuple[str, str]] = []

    # A subscription that ran out: the number is removed here, on its first
    # message after the end date, and told how to come back.
    if _billing is not None and _engine.cars.is_registered(sender):
        ended = _billing.expired(sender)
        if ended:
            await _send_all(sender, ended, extra_notify)
            return Response(status_code=200, content="ok")

    is_registered = _engine.cars.is_registered(sender)
    is_admin = _engine.is_admin(sender)

    # Admin onboarding commands (approve/deny/pending/help).
    from .onboarding import parse_admin_command
    admin_cmd, admin_args = parse_admin_command(text or "")

    # Owner statistics: site counts and the subscription funnel (analytics.py).
    stats = re.match(r"^/?stats(?:\s+(\d{1,3}))?$", (text or "").strip().lower())
    if is_admin and stats:
        from .analytics import report
        days = min(int(stats.group(1) or 7), 400) or 7
        await _send_all(sender, report(_settings, days=days), extra_notify)
        return Response(status_code=200, content="ok")

    if is_admin and admin_cmd:
        try:
            from .cars import normalize_phone
            approved = normalize_phone(admin_args[0]) if admin_cmd == "approve" and admin_args else ""
            was_registered = bool(approved) and _engine.cars.is_registered(approved)
            reply = _engine.handle_admin_command(admin_cmd, admin_args)
            # If a user was just approved, welcome them too, in their language.
            if approved and not was_registered and _engine.cars.is_registered(approved):
                extra_notify.append((
                    approved, t("onboard.approved", _engine.languages.of(approved)),
                ))
        except Exception:  # pragma: no cover
            log.exception("admin command failed")
            reply = t("admin.failed")
    elif not is_registered:
        # A former subscriber (car removed when the subscription ended) may
        # restart with the same ledger; anything else gets the ended notice.
        if _billing is not None and _billing.returning(sender):
            reply = await _subscription_reply(sender, text or "") or _billing.ended_text(sender)
            await _send_all(sender, reply, extra_notify)
            return Response(status_code=200, content="ok")
        # Onboarding: record a join request and notify admins.
        if not _settings.onboarding_enabled:
            log.warning("Ignoring message from unregistered number %s", sender)
            return Response(status_code=200, content="ok")
        if _billing is not None:
            # With paid signup, the website is the way in; no join requests.
            await _send_all(sender, t("onboard.signup", url=_settings.signup_url), extra_notify)
            return Response(status_code=200, content="ok")
        if _engine.onboarding.has(sender):
            reply = t("onboard.pending")
        else:
            _engine.register_join_request(sender, text or "(non-text message)")
            reply = t("onboard.requested")
            for admin in _settings.admin_list():
                extra_notify.append((admin, t(
                    "admin.join_request", _engine.languages.of(admin),
                    sender=sender, message=(text or "")[:60],
                )))
        # send onboarding reply + notifications below
        await _send_all(sender, reply, extra_notify)
        return Response(status_code=200, content="ok")
    else:
        sub_reply = await _subscription_reply(sender, text or "") if _billing is not None else None
        if sub_reply is not None:
            await _send_all(sender, sub_reply, extra_notify)
            return Response(status_code=200, content="ok")
        # Car selection for people who report for several cars.
        try:
            user_reply = _engine.handle_user_command(text or "", sender)
        except (EngineError, UnknownCarError) as exc:
            user_reply = str(exc)
        if user_reply is not None:
            await _send_all(sender, user_reply, extra_notify)
            return Response(status_code=200, content="ok")
        # Spreadsheet request: acknowledge now, generate and send in the
        # background (a plugin may take a while; Meta wants a fast 200).
        try:
            export_req = _engine.parse_export_command(text or "", sender)
        except (EngineError, UnknownCarError) as exc:
            await _send_all(sender, t("export.could_not", error=exc), extra_notify)
            return Response(status_code=200, content="ok")
        if export_req is not None:
            await _ack_export(sender, message_id)
            return Response(
                status_code=200, content="ok",
                background=BackgroundTask(_run_export, sender, export_req),
            )
        # Paused past the paid period: exports and commands above still work,
        # new trips are refused until the subscriber resumes.
        paused = _billing.paused(sender) if _billing is not None else None
        if paused:
            await _send_all(sender, paused, extra_notify)
            return Response(status_code=200, content="ok")
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
                reply = t("trip.usage")
        except (ParseError, EngineError, UnknownCarError) as exc:
            reply = t("trip.could_not", error=exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("Unexpected error handling message")
            reply = t("trip.internal")

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
    admins = set(_settings.admin_list())
    for to, body in extra:
        if to in admins:
            await _alert(to, body)
            continue
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
        sender, t("export.generating"), graph_url=graph_url,
    )


async def _run_export(sender: str, req: ExportRequest) -> None:
    """Generate one workbook per requested year and send each as a document."""
    graph_url = f"https://graph.facebook.com/{_settings.whatsapp_graph_version}"
    token, pid = _settings.whatsapp_token, _settings.whatsapp_phone_number_id

    set_current(_engine.languages.of(sender))  # runs after the response

    async def tell(text: str) -> None:
        await whatsapp.send_message(token, pid, sender, text, graph_url=graph_url)

    for year in req.years:
        try:
            result = await asyncio.to_thread(_engine.exporter.generate, req.car.car_id, year)
        except ExportError as exc:
            await tell(t("export.failed", year=year, error=exc))
            continue
        except Exception:  # pragma: no cover - defensive
            log.exception("export failed for %s %s", req.car.car_id, year)
            await tell(t("export.failed_internal", year=year))
            continue
        media_id = await whatsapp.upload_media(token, pid, result.path, graph_url=graph_url)
        if not media_id:
            await tell(t("export.upload_failed", year=year))
            continue
        caption = t("export.caption", label=req.car.label, year=year, rows=result.rows)
        if result.handlers:
            caption += t("export.plugins", n=result.handlers, s="s" if result.handlers != 1 else "")
        sent = await whatsapp.send_document(
            token, pid, sender, media_id, result.path.name, caption=caption,
            graph_url=graph_url,
        )
        if not sent:
            await tell(t("export.not_sent", year=year))
        else:
            log.info("Export %s/%s sent to %s (%s rows).", req.car.car_id, year, sender, result.rows)
