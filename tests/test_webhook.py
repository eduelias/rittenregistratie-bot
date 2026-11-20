"""Webhook-level tests, including the ping/pong connectivity check."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Configure an isolated environment before importing the app module.
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "locations.yaml").write_text("Home: {address: 'A St'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        "car:\n  label: C\n  seed_address: Home\n  seed_odometer: 100\n"
        "  phones: ['31600000000']\n"
    )
    monkeypatch.setenv("RIT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("RIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RIT_WHATSAPP_APP_SECRET", "")  # signature check disabled
    monkeypatch.setenv("RIT_WHATSAPP_TOKEN", "")  # send is a no-op
    monkeypatch.setenv("RIT_WHATSAPP_PHONE_NUMBER_ID", "")

    import rittenregistratie.config as config
    config._settings = None  # reset cached settings
    import rittenregistratie.main as main
    importlib.reload(main)

    from fastapi.testclient import TestClient
    return TestClient(main.app)


def _msg(text, sender="31600000000"):
    return {
        "entry": [
            {"changes": [{"value": {"messages": [
                {"from": sender, "type": "text", "text": {"body": text}}
            ]}}]}
        ]
    }


def test_ping_returns_pong_without_logging(client, tmp_path):
    resp = client.post("/webhook", json=_msg("ping"))
    assert resp.status_code == 200
    # ping must not create any trip log
    assert not list((tmp_path / "data").glob("trips-*.xlsx"))


def test_ping_case_insensitive(client):
    assert client.post("/webhook", json=_msg("PING")).status_code == 200


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_extract_statuses():
    from rittenregistratie.whatsapp import extract_statuses
    body = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.X", "status": "delivered", "recipient_id": "31612345678"},
        {"id": "wamid.Y", "status": "failed", "recipient_id": "31612345678",
         "errors": [{"title": "Undeliverable"}]},
    ]}}]}]}
    out = extract_statuses(body)
    assert out[0]["status"] == "delivered"
    assert out[1]["status"] == "failed" and out[1]["error"] == "Undeliverable"


def test_extract_statuses_empty():
    from rittenregistratie.whatsapp import extract_statuses
    assert extract_statuses({"entry": [{"changes": [{"value": {}}]}]}) == []


def test_graph_version_default():
    from rittenregistratie.config import Settings
    s = Settings(config_dir="config")
    assert s.whatsapp_graph_version == "v25.0"


def test_send_message_uses_graph_url(monkeypatch):
    import asyncio
    import rittenregistratie.whatsapp as wa
    captured = {}

    class FakeResp:
        status_code = 200
        text = "{}"

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            captured["url"] = url
            return FakeResp()

    monkeypatch.setattr(wa.httpx, "AsyncClient", FakeClient)
    ok = asyncio.run(wa.send_message("t", "PID", "31600", "hi",
                                     graph_url="https://graph.facebook.com/v25.0"))
    assert ok
    assert captured["url"] == "https://graph.facebook.com/v25.0/PID/messages"


def test_unregistered_number_gets_onboarding(client, tmp_path):
    # An unregistered number should get an onboarding reply and a pending request.
    resp = client.post("/webhook", json=_msg("hello", sender="31699999999"))
    assert resp.status_code == 200
    import json
    ob = json.loads((tmp_path / "data" / "onboarding.json").read_text())
    assert "31699999999" in ob


# --- reactions ---------------------------------------------------------------

def test_extract_messages_carries_id():
    from rittenregistratie.whatsapp import extract_messages
    body = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "316", "id": "wamid.ABC", "type": "text", "text": {"body": "hi"}}
    ]}}]}]}
    assert extract_messages(body) == [{"from": "316", "id": "wamid.ABC", "text": "hi"}]


def test_extract_messages_takes_every_message_and_skips_reactions():
    from rittenregistratie.whatsapp import extract_messages
    text = lambda i, body: {"from": "316", "id": i, "type": "text", "text": {"body": body}}  # noqa: E731
    body = {"entry": [
        {"changes": [{"value": {"messages": [
            text("a", "150 Office"),
            {"from": "316", "id": "r", "type": "reaction", "reaction": {"emoji": "x"}},
            text("b", "180 Home")]}}]},
        {"changes": [{"value": {"statuses": [{"id": "s"}]}},
                     {"value": {"messages": [{"from": "317", "id": "c", "type": "image"}]}}]},
    ]}
    assert [(m["id"], m["text"]) for m in extract_messages(body)] == [
        ("a", "150 Office"), ("b", "180 Home"), ("c", None)]
    for junk in ({}, {"entry": None}, {"entry": [{"changes": [{"value": "x"}]}]}, {"entry": [5]}):
        assert extract_messages(junk) == []


# --- who may post -------------------------------------------------------------

def _signed(secret: str, raw: bytes) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_verify_signature():
    from rittenregistratie.whatsapp import verify_signature
    raw = b'{"entry": []}'
    assert verify_signature("s3cret", raw, _signed("s3cret", raw))
    assert not verify_signature("s3cret", raw, _signed("other", raw))
    assert not verify_signature("s3cret", raw + b" ", _signed("s3cret", raw))   # body changed
    assert not verify_signature("s3cret", raw, "")
    assert not verify_signature("s3cret", raw, "sha256=é")                     # no crash
    # Nothing to check against: refused, unless development says otherwise.
    assert not verify_signature("", raw, _signed("", raw))
    assert verify_signature("", raw, "", allow_unsigned=True)


@pytest.fixture
def signed_client(client, monkeypatch):
    """The same app with an app secret set, as in production."""
    import rittenregistratie.main as main
    monkeypatch.setattr(main._settings, "whatsapp_app_secret", "s3cret")
    monkeypatch.setattr(main._settings, "whatsapp_allow_unsigned", False)
    return client


def test_webhook_refuses_what_meta_did_not_sign(signed_client, tmp_path):
    import json
    raw = json.dumps(_msg("150 Home")).encode()
    headers = {"Content-Type": "application/json"}
    assert signed_client.post("/webhook", content=raw, headers=headers).status_code == 403
    bad = dict(headers, **{"X-Hub-Signature-256": _signed("guess", raw)})
    assert signed_client.post("/webhook", content=raw, headers=bad).status_code == 403
    assert not (tmp_path / "data" / "raw-ledger-car.jsonl").exists()
    good = dict(headers, **{"X-Hub-Signature-256": _signed("s3cret", raw)})
    assert signed_client.post("/webhook", content=raw, headers=good).status_code == 200
    assert (tmp_path / "data" / "raw-ledger-car.jsonl").exists()


def test_no_secret_and_no_development_switch_refuses_everything(client, monkeypatch):
    import rittenregistratie.main as main
    monkeypatch.setattr(main._settings, "whatsapp_allow_unsigned", False)
    assert client.post("/webhook", json=_msg("ping")).status_code == 403


def test_a_driver_cannot_run_admin_commands(client, tmp_path):
    cars = (tmp_path / "config" / "cars.yaml")
    before = cars.read_text()
    for command in ("approve 31699999999 Polo 10 Home", "remove 31600000000", "remove car",
                    "assign 31699999999 car", "pending", "list"):
        assert client.post("/webhook", json=_msg(command)).status_code == 200
    assert cars.read_text() == before


def test_one_delivery_with_two_trips_logs_both(client, tmp_path):
    body = _msg("150 Home")
    body["entry"][0]["changes"][0]["value"]["messages"].append(
        {"from": "31600000000", "type": "text", "text": {"body": "180 Home"}})
    assert client.post("/webhook", json=body).status_code == 200
    rows = (tmp_path / "data" / "raw-ledger-car.jsonl").read_text().splitlines()
    assert len(rows) == 2


def test_a_delivery_meta_repeats_is_handled_once(client, monkeypatch):
    import rittenregistratie.main as main
    handled = []

    async def spy(msg):
        handled.append(msg["id"])
        from fastapi import Response
        return Response(status_code=200)

    monkeypatch.setattr(main, "_handle_message", spy)
    body = _msg("ping")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.SAME"
    client.post("/webhook", json=body)
    client.post("/webhook", json=body)
    assert handled == ["wamid.SAME"]


def test_a_message_that_crashes_still_gets_meta_a_200(client, monkeypatch):
    import rittenregistratie.main as main

    async def boom(msg):
        raise RuntimeError("bug")

    monkeypatch.setattr(main, "_handle_message", boom)
    assert client.post("/webhook", json=_msg("150 Home")).status_code == 200


def test_nothing_internal_is_served(client):
    for path in ("/selftest", "/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path
    assert client.get("/health").headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/health").headers["X-Frame-Options"] == "DENY"


def test_the_hook_needs_its_secret(client, monkeypatch):
    import rittenregistratie.main as main
    assert client.post("/hooks/homeassistant/car", json={"odometer": 150}).status_code == 403
    monkeypatch.setattr(main._settings, "hook_secret", "h00k")
    wrong = client.post("/hooks/homeassistant/car", json={}, headers={"X-Hook-Secret": "wrong"})
    assert wrong.status_code == 403


def test_send_reaction_payload(monkeypatch):
    import asyncio
    import rittenregistratie.whatsapp as wa
    captured = {}

    class FakeResp:
        status_code = 200
        text = "{}"

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw["json"]
            return FakeResp()

    monkeypatch.setattr(wa.httpx, "AsyncClient", FakeClient)
    ok = asyncio.run(wa.send_reaction("t", "PID", "31600", "wamid.X",
                                      graph_url="https://graph.facebook.com/v25.0"))
    assert ok
    assert captured["url"] == "https://graph.facebook.com/v25.0/PID/messages"
    assert captured["json"]["type"] == "reaction"
    assert captured["json"]["reaction"] == {"message_id": "wamid.X", "emoji": "\U0001F44D"}
    assert not asyncio.run(wa.send_reaction("t", "PID", "31600", ""))


def _msg_with_id(text, msg_id="wamid.IN", sender="31600000000"):
    body = _msg(text, sender)
    body["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = msg_id
    return body


@pytest.fixture
def reaction_client(tmp_path, monkeypatch):
    monkeypatch.setenv("RIT_REPLY_MODE", "reaction")
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "locations.yaml").write_text("Home: {address: 'A St'}\nOffice: {address: 'B St'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        "car:\n  label: C\n  seed_address: Home\n  seed_odometer: 100\n"
        "  phones: ['31600000000']\n"
    )
    monkeypatch.setenv("RIT_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("RIT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RIT_WHATSAPP_APP_SECRET", "")
    monkeypatch.setenv("RIT_WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("RIT_WHATSAPP_PHONE_NUMBER_ID", "pid")

    import rittenregistratie.config as config
    config._settings = None
    import rittenregistratie.main as main
    importlib.reload(main)

    sent = {"reactions": [], "texts": []}

    async def fake_reaction(token, pid, to, message_id, emoji="\U0001F44D", graph_url=""):
        sent["reactions"].append((to, message_id, emoji))
        return True

    async def fake_text(token, pid, to, text, graph_url=""):
        sent["texts"].append((to, str(text)))
        return True

    monkeypatch.setattr(main.whatsapp, "send_reaction", fake_reaction)
    monkeypatch.setattr(main.whatsapp, "send_message", fake_text)
    from fastapi.testclient import TestClient
    return TestClient(main.app), sent


def test_reaction_mode_thumbs_up_only_on_logged_trip(reaction_client):
    client, sent = reaction_client
    assert client.post("/webhook", json=_msg_with_id("140 Office", "wamid.T1")).status_code == 200
    assert sent["reactions"] == [("31600000000", "wamid.T1", "\U0001F44D")]
    assert sent["texts"] == []


def test_reaction_mode_still_texts_prompts_and_pong(reaction_client):
    client, sent = reaction_client
    client.post("/webhook", json=_msg_with_id("ping", "wamid.P"))
    client.post("/webhook", json=_msg_with_id("150 Gym", "wamid.G"))  # unknown -> prompt
    assert sent["reactions"] == []
    assert [t for _, t in sent["texts"]] == ["pong"] + [sent["texts"][1][1]]
    assert "don't have an address" in sent["texts"][1][1].lower()
    # Answering the prompt logs the trip AND reports the learned address.
    client.post("/webhook", json=_msg_with_id("Sportlaan 1", "wamid.A"))
    assert ("31600000000", "wamid.A", "\U0001F44D") in sent["reactions"]
    assert any("Saved address" in t for _, t in sent["texts"])


def test_reaction_mode_falls_back_to_text_when_reaction_fails(reaction_client, monkeypatch):
    client, sent = reaction_client
    import rittenregistratie.main as main

    async def failing_reaction(*a, **k):
        return False

    monkeypatch.setattr(main.whatsapp, "send_reaction", failing_reaction)
    client.post("/webhook", json=_msg_with_id("140 Office", "wamid.T1"))
    assert len(sent["texts"]) == 1 and "40 km" in sent["texts"][0][1]


# --- on-demand export --------------------------------------------------------

def test_upload_media_and_send_document(monkeypatch, tmp_path):
    import asyncio
    import rittenregistratie.whatsapp as wa
    f = tmp_path / "trips-car-2026.xlsx"
    f.write_bytes(b"PK fake")
    captured = []

    class FakeResp:
        status_code = 200
        text = '{"id": "MEDIA1"}'
        def json(self): return {"id": "MEDIA1"}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **kw):
            captured.append((url, kw))
            return FakeResp()

    monkeypatch.setattr(wa.httpx, "AsyncClient", FakeClient)
    g = "https://graph.facebook.com/v25.0"
    assert asyncio.run(wa.upload_media("t", "PID", f, graph_url=g)) == "MEDIA1"
    url, kw = captured[0]
    assert url == f"{g}/PID/media"
    assert kw["data"] == {"messaging_product": "whatsapp", "type": wa.XLSX_MIME}
    assert kw["files"]["file"][0] == "trips-car-2026.xlsx"
    assert asyncio.run(wa.send_document("t", "PID", "316", "MEDIA1", "trips-car-2026.xlsx",
                                        caption="cap", graph_url=g))
    url, kw = captured[1]
    assert url == f"{g}/PID/messages"
    assert kw["json"]["type"] == "document"
    assert kw["json"]["document"] == {"id": "MEDIA1", "filename": "trips-car-2026.xlsx", "caption": "cap"}
    assert not asyncio.run(wa.send_document("t", "PID", "316", "", "x.xlsx"))


@pytest.fixture
def export_client(reaction_client, monkeypatch):
    client, sent = reaction_client
    import rittenregistratie.main as main
    sent["uploads"] = []
    sent["documents"] = []

    async def fake_upload(token, pid, path, mime=None, graph_url=""):
        sent["uploads"].append(str(path))
        return "MEDIA-1"

    async def fake_document(token, pid, to, media_id, filename, caption="", graph_url=""):
        sent["documents"].append((to, media_id, filename, caption))
        return True

    monkeypatch.setattr(main.whatsapp, "upload_media", fake_upload)
    monkeypatch.setattr(main.whatsapp, "send_document", fake_document)
    return client, sent


def test_excel_command_sends_document(export_client, tmp_path):
    client, sent = export_client
    client.post("/webhook", json=_msg_with_id("140 Office", "wamid.T1"))
    resp = client.post("/webhook", json=_msg_with_id("excel", "wamid.X"))
    assert resp.status_code == 200
    # hourglass ack on the command, then the file (TestClient runs background tasks).
    assert ("31600000000", "wamid.X", "\u23F3") in sent["reactions"]
    assert len(sent["documents"]) == 1
    to, media_id, filename, caption = sent["documents"][0]
    assert (to, media_id) == ("31600000000", "MEDIA-1")
    assert filename.startswith("trips-car-") and filename.endswith(".xlsx")
    assert "1 trips" in caption
    assert sent["uploads"][0].endswith(f"exports/{filename}")
    # No per-trip spreadsheet was ever written to data/.
    assert [p.name for p in (tmp_path / "data").glob("*.xlsx")] == []


def test_excel_command_reports_missing_year_and_bad_args(export_client):
    client, sent = export_client
    client.post("/webhook", json=_msg_with_id("excel 2019", "wamid.X"))
    assert sent["documents"] == []
    assert any("Export 2019 failed" in t and "No trips" in t for _, t in sent["texts"])
    client.post("/webhook", json=_msg_with_id("excel othercar", "wamid.Y"))
    assert any("Could not export" in t and "Unknown car 'othercar'" in t for _, t in sent["texts"])


# --- multi-car users over the webhook -----------------------------------------

def test_cars_and_car_commands_over_webhook(reaction_client, tmp_path):
    client, sent = reaction_client
    import rittenregistratie.main as main
    cfg = tmp_path / "config"
    (cfg / "cars.yaml").write_text(
        "car:\n  label: C\n  seed_address: Home\n  seed_odometer: 100\n  phones: ['31600000000']\n"
        "van:\n  label: V\n  seed_address: Home\n  seed_odometer: 500\n  phones: ['31600000000']\n"
    )
    main._engine.reload_cars()
    client.post("/webhook", json=_msg_with_id("140 Office", "wamid.1"))
    assert any("several cars" in t for _, t in sent["texts"])
    client.post("/webhook", json=_msg_with_id("car van", "wamid.2"))
    assert any(t == "Active car: V [van]." for _, t in sent["texts"])
    client.post("/webhook", json=_msg_with_id("540 Office", "wamid.3"))
    assert ("31600000000", "wamid.3", "\U0001F44D") in sent["reactions"]
    client.post("/webhook", json=_msg_with_id("cars", "wamid.4"))
    assert any("V [van] (active)" in t for _, t in sent["texts"])


def test_the_24_hour_window_closes_an_hour_early(tmp_path):
    """Sending right at the limit risks a refused message, so the bot stops at 23 hours."""
    from datetime import datetime, timedelta
    from rittenregistratie.window import InboundWindow
    w = InboundWindow(tmp_path / "last-inbound.json")
    t0 = datetime(2026, 11, 20, 9, 0)
    assert not w.is_open("31612345678", t0)                     # never wrote
    w.seen("31612345678", t0)
    assert w.is_open("31612345678", t0 + timedelta(hours=22, minutes=59))
    assert not w.is_open("31612345678", t0 + timedelta(hours=23, minutes=1))
    w.seen("31600000009", t0 + timedelta(days=3))               # someone else, days later
    import json
    assert list(json.loads((tmp_path / "last-inbound.json").read_text())) == ["31600000009"]
