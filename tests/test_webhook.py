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

def test_extract_message_carries_id():
    from rittenregistratie.whatsapp import extract_message
    body = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "316", "id": "wamid.ABC", "type": "text", "text": {"body": "hi"}}
    ]}}]}]}
    assert extract_message(body) == {"from": "316", "id": "wamid.ABC", "text": "hi"}


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
