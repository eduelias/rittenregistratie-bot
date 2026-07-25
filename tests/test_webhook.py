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
