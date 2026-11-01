"""Dutch and English, chosen per number in the bot; default Dutch."""
import asyncio
import importlib
import re

import pytest

from rittenregistratie import i18n
from rittenregistratie.i18n import CATALOG, LanguageStore, parse_language_command, t

BOT_LANGUAGE = "nl"   # the real default: these tests start from Dutch
OWNER, DRIVER, OTHER = "31600000001", "31600000002", "31600000003"


def test_every_text_exists_in_both_languages_with_the_same_placeholders():
    fields = re.compile(r"{(\w+)}")
    for key, (nl, en) in CATALOG.items():
        assert nl.strip() and en.strip(), key
        assert set(fields.findall(nl)) == set(fields.findall(en)), key


def test_language_command_parsing():
    assert parse_language_command("english") == (True, "en")
    assert parse_language_command("Nederlands") == (True, "nl")
    assert parse_language_command("taal en") == (True, "en")
    assert parse_language_command("/language nl") == (True, "nl")
    assert parse_language_command("taal") == (True, None)
    assert parse_language_command("en") == (False, None)        # too short to be a command
    assert parse_language_command("45230 English Bay") == (False, None)


def test_store_and_dates(tmp_path):
    store = LanguageStore(tmp_path / "languages.json")
    assert store.of("+31 6 0000 0002") == "nl"
    store.set("31600000002", "en")
    assert store.of("+31 6 0000 0002") == "en"
    assert i18n.date_text("2026-11-14", "nl") == "14 november 2026"
    assert i18n.date_text("2026-11-14", "en") == "14 November 2026"
    assert i18n.money("4.95", "nl") == "4,95" and i18n.money("4.95", "en") == "4.95"


@pytest.fixture
def app(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "locations.yaml").write_text("home: {address: 'A St 1'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        f"golf:\n  label: Golf\n  seed_address: home\n  seed_odometer: 100\n"
        f"  phones: ['{DRIVER}', '{OTHER}']\n"
        f"mine:\n  label: Mine\n  seed_odometer: 1\n  phones: ['{OWNER}']\n")
    for k, v in {"RIT_CONFIG_DIR": str(cfg), "RIT_DATA_DIR": str(tmp_path / "data"),
                 "RIT_WHATSAPP_APP_SECRET": "", "RIT_WHATSAPP_TOKEN": "",
                 "RIT_WHATSAPP_PHONE_NUMBER_ID": "", "RIT_ADMIN_NUMBERS": OWNER,
                 "RIT_REPLY_MODE": "text", "RIT_HOOK_SECRET": "s3cret",
                 "RIT_EVENT_PLUGINS": ""}.items():
        monkeypatch.setenv(k, v)
    import rittenregistratie.config as config
    config._settings = None
    import rittenregistratie.main as main
    importlib.reload(main)
    sent = []

    async def fake_send(token, pid, to, body, **kw):
        sent.append((to, str(body)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_message", fake_send)
    from fastapi.testclient import TestClient
    return TestClient(main.app), main, sent


def _wa(text, sender):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "type": "text", "text": {"body": text}}]}}]}]}


def test_default_dutch_then_switch_to_english_and_back(app):
    client, main, sent = app
    client.post("/webhook", json=_wa("150 kantoor", DRIVER))
    assert "Ik heb nog geen adres voor 'kantoor'" in sent[-1][1]
    client.post("/webhook", json=_wa("annuleer", DRIVER))
    client.post("/webhook", json=_wa("english", DRIVER))
    assert "I'll reply in English" in sent[-1][1]
    client.post("/webhook", json=_wa("160 office", DRIVER))
    assert "I don't have an address for 'office'" in sent[-1][1]
    client.post("/webhook", json=_wa("cancel", DRIVER))
    client.post("/webhook", json=_wa("taal", DRIVER))
    assert sent[-1][1].startswith("I reply in English")
    client.post("/webhook", json=_wa("taal nl", DRIVER))
    assert "in het Nederlands" in sent[-1][1]
    client.post("/webhook", json=_wa("help", DRIVER))
    assert sent[-1][1].startswith("Welkom bij RittenRegistratie (Golf)")
    # Only that number changed: the other driver of the car is still Dutch.
    client.post("/webhook", json=_wa("help", OTHER))
    assert sent[-1][1].startswith("Welkom")


def test_a_trip_reply_and_errors_follow_the_language(app):
    client, main, sent = app
    client.post("/webhook", json=_wa("english", DRIVER))
    client.post("/webhook", json=_wa("170 home private", DRIVER))
    assert "(70 km, private)" in sent[-1][1] and "Odometer 170." in sent[-1][1]
    client.post("/webhook", json=_wa("150 home", DRIVER))
    assert "is lower than last recorded 170" in sent[-1][1]
    client.post("/webhook", json=_wa("180 home privé", OTHER))
    assert "(10 km, privé)" in sent[-1][1] and "Kilometerstand 180." in sent[-1][1]


def test_a_car_logged_trip_reaches_each_driver_in_their_language(app, monkeypatch):
    client, main, sent = app
    main._engine.languages.set(DRIVER, "en")
    from rittenregistratie.events import hook_event
    from rittenregistratie.models import VehicleTripReport
    monkeypatch.setattr(main._engine.cars.get("golf"), "event_plugins", ["fake"])
    bus = main._engine.bus_for("golf")
    bus.on(hook_event("fake"), lambda p: VehicleTripReport(end_odo=250, place="home"))
    r = client.post("/hooks/fake/golf", json={}, headers={"X-Hook-Secret": "s3cret"})
    assert r.json()["status"] == "logged"
    by = dict(sent)
    assert "Odometer 250." in by[DRIVER] and "business" in by[DRIVER]
    assert "Kilometerstand 250." in by[OTHER] and "zakelijk" in by[OTHER]


def test_template_tries_the_driver_language_then_the_configured_one(app, monkeypatch):
    client, main, sent = app
    tried = []

    async def fake_template(token, pid, to, name, params, language="en", graph_url=""):
        tried.append((language, params[4]))
        return language == "en"

    monkeypatch.setattr(main.whatsapp, "send_template", fake_template)
    monkeypatch.setattr(main._settings, "whatsapp_trip_template", "trip_logged")
    from rittenregistratie.models import Reply
    reply = Reply("x", facts={"label": "Golf", "origin": "A", "destination": "B", "km": 3,
                              "purpose": "private", "odometer": 9})
    assert asyncio.run(main._notify_trip(OTHER, reply, "g", "nl"))
    assert tried == [("nl", "privé"), ("en", "private")]


def test_admin_alerts_and_join_requests_use_the_admin_language(app):
    client, main, sent = app
    main._engine.languages.set(OWNER, "en")
    main._settings.onboarding_enabled = True
    client.post("/webhook", json=_wa("hallo", "31699999999"))
    by = dict(sent)
    assert "Je nummer is nog niet geregistreerd" in by["31699999999"]
    assert by[OWNER].startswith("New join request from 31699999999")
    client.post("/webhook", json=_wa("approve 31699999999 Polo 1000", OWNER))
    by = dict(sent)
    assert by[OWNER].startswith("Approved 31699999999")
    assert by["31699999999"].startswith("Je bent goedgekeurd")


def test_stats_are_english_even_for_a_dutch_owner(app):
    client, main, sent = app
    client.post("/webhook", json=_wa("stats", OWNER))
    assert "page views" in sent[-1][1]
