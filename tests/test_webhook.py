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
