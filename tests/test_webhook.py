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
