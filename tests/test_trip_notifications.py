"""Trips logged by the car are announced with a template, not a text.

A plain text is only deliverable within 24 hours of the driver's last message
to the bot. A trip the car reported is exactly the message nobody prompted, and
with the car doing the logging the window rarely reopens: Meta accepts the
message (200) and then reports it `failed (Re-engagement message)`, error
131047. Seven notifications in a row went that way before anyone noticed.
"""
import asyncio
import logging

import pytest

from rittenregistratie import main, whatsapp
from rittenregistratie.models import Reply


class Posts:
    """Capture every payload the client would have posted."""

    def __init__(self, refuse=()):
        self.payloads, self.refuse = [], set(refuse)

    async def __call__(self, token, pid, payload, graph_url, what):
        self.payloads.append(payload)
        return payload["type"] not in self.refuse


@pytest.fixture
def posts(monkeypatch):
    p = Posts()
    monkeypatch.setattr(whatsapp, "_post_message", p)
    return p


def _reply():
    return Reply("[Car] home -> SBP (44 km, business).\nOdometer 20900.", logged=True,
                 facts={"label": "Car", "origin": "home", "destination": "SBP",
                        "km": 44, "purpose": "business", "odometer": 20900})


def test_a_template_carries_the_six_facts_in_order(posts):
    ok = asyncio.run(whatsapp.send_template(
        "tok", "pid", "316", "trip_logged", ["Car", "home", "SBP", 44, "business", 20900]))
    assert ok
    body = posts.payloads[0]
    assert body["type"] == "template"
    assert body["template"]["name"] == "trip_logged"
    assert body["template"]["language"] == {"code": "en"}
    params = body["template"]["components"][0]["parameters"]
    assert [p["text"] for p in params] == ["Car", "home", "SBP", "44", "business", "20900"]
    assert all(p["type"] == "text" for p in params)


def test_the_car_s_trip_goes_out_as_the_configured_template(posts, monkeypatch):
    monkeypatch.setattr(main._settings, "whatsapp_trip_template", "trip_logged")
    monkeypatch.setattr(main._settings, "whatsapp_token", "tok")
    monkeypatch.setattr(main._settings, "whatsapp_phone_number_id", "pid")
    assert asyncio.run(main._notify_trip("316", _reply(), "https://g"))
    assert [p["type"] for p in posts.payloads] == ["template"]
    params = posts.payloads[0]["template"]["components"][0]["parameters"]
    assert params[0]["text"] == "Car" and params[-1]["text"] == "20900"


def test_without_a_template_the_plain_text_still_goes_out(posts, monkeypatch):
    monkeypatch.setattr(main._settings, "whatsapp_trip_template", "")
    monkeypatch.setattr(main._settings, "whatsapp_token", "tok")
    monkeypatch.setattr(main._settings, "whatsapp_phone_number_id", "pid")
    assert asyncio.run(main._notify_trip("316", _reply(), "https://g"))
    assert [p["type"] for p in posts.payloads] == ["text"]
    assert "Odometer 20900" in posts.payloads[0]["text"]["body"]


def test_a_refused_template_falls_back_to_text_rather_than_silence(monkeypatch, caplog):
    """Not yet approved, wrong parameter count - the driver still hears something."""
    p = Posts(refuse={"template"})
    monkeypatch.setattr(whatsapp, "_post_message", p)
    monkeypatch.setattr(main._settings, "whatsapp_trip_template", "trip_logged")
    monkeypatch.setattr(main._settings, "whatsapp_token", "tok")
    monkeypatch.setattr(main._settings, "whatsapp_phone_number_id", "pid")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(main._notify_trip("316", _reply(), "https://g"))
    assert [x["type"] for x in p.payloads] == ["template", "text"]
    assert any("refused" in m for m in caplog.messages)


def test_a_reply_without_facts_uses_text_even_with_a_template(posts, monkeypatch):
    """Only a logged trip has facts; anything else has nothing to fill a template with."""
    monkeypatch.setattr(main._settings, "whatsapp_trip_template", "trip_logged")
    monkeypatch.setattr(main._settings, "whatsapp_token", "tok")
    monkeypatch.setattr(main._settings, "whatsapp_phone_number_id", "pid")
    asyncio.run(main._notify_trip("316", Reply("Already logged.", logged=False), "https://g"))
    assert [p["type"] for p in posts.payloads] == ["text"]


def test_a_logged_trip_reply_carries_its_facts(tmp_path):
    from datetime import datetime
    from rittenregistratie.config import Settings
    from rittenregistratie.engine import Engine
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "locations.yaml").write_text(
        "Home: {address: 'A St'}\nOffice: {address: 'B St'}\n")
    (tmp_path / "config" / "cars.yaml").write_text(
        "car:\n  label: 'Test car'\n  seed_address: Home\n  seed_odometer: 1000\n"
        "  phones: ['31612345678']\n")
    eng = Engine(Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                          trajectory_provider="maps_link", whatsapp_app_secret=""))
    reply = eng.handle_text("1044 Office", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    assert reply.facts == {"label": "Test car", "origin": "Home", "destination": "Office",
                           "km": 44, "purpose": "business", "odometer": 1044}


def test_a_status_receipt_keeps_the_error_code():
    body = {"entry": [{"id": "2515596578909103", "changes": [{"value": {"statuses": [{
        "id": "wamid.X", "status": "failed", "recipient_id": "316",
        "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    st = whatsapp.extract_statuses(body)[0]
    assert st["code"] == 131047 and st["error"] == "Re-engagement message"
    assert whatsapp.extract_waba_id(body) == "2515596578909103"


def test_the_window_error_is_explained_in_the_journal(caplog):
    """A 200 from Meta followed by a quiet 'failed' is how seven trips vanished."""
    from fastapi.testclient import TestClient
    main._waba_logged = False
    body = {"object": "whatsapp_business_account", "entry": [{"id": "2515596578909103",
            "changes": [{"value": {"statuses": [{
                "id": "wamid.X", "status": "failed", "recipient_id": "316",
                "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
    with caplog.at_level(logging.INFO):
        TestClient(main.app).post("/webhook", json=body)
    text = "\n".join(caplog.messages)
    assert "131047" in text and "RIT_WHATSAPP_TRIP_TEMPLATE" in text
    assert "2515596578909103" in text, "the WABA id was not logged"
