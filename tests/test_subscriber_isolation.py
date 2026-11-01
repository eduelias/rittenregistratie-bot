"""A paying subscriber's car never gets the operator's plugins or places."""
from datetime import datetime

import pytest

from rittenregistratie import events
from rittenregistratie.config import Settings
from rittenregistratie.engine import Engine

OWNER = "31600000001"
SUB = "31612345678"


class _EP:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def load(self):
        return self._fn


@pytest.fixture
def loaded(monkeypatch):
    """Two installed event plugins; records which car buses they joined."""
    joined = []

    def reg(name):
        def register(bus):
            joined.append(name)
            bus.on("export.pre_generate", lambda p: p)
        return register

    fake = [_EP("reallocate", reg("reallocate")), _EP("public", reg("public"))]
    monkeypatch.setattr(events, "entry_points", lambda group: fake)
    return joined


def _settings(tmp_path, cars_yaml, **kw):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "locations.yaml").write_text(
        "home: {address: 'Buitenhof 128, Almere'}\noffice: {address: 'Keizersgracht 1'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(cars_yaml)
    base = dict(data_dir=tmp_path / "data", config_dir=cfg, trajectory_provider="maps_link",
                private_cap_plugin="warn", whatsapp_app_secret="", admin_numbers=OWNER)
    base.update(kw)
    return Settings(**base)


CARS = (
    "mercedes:\n  label: Mercedes\n  seed_address: Home\n  seed_odometer: 100\n"
    f"  phones: ['{OWNER}']\n  event_plugins: [reallocate]\n"
    "golf:\n  label: Golf\n  seed_address: 'Dorpsstraat 1, Utrecht'\n  seed_odometer: 5000\n"
    f"  phones: ['{SUB}']\n  subscription: sig123\n"
)


def test_subscriber_does_not_inherit_the_global_plugins(tmp_path, loaded):
    eng = Engine(_settings(tmp_path, CARS, event_plugins="reallocate"))
    eng.bus_for("golf")
    assert eng.loaded_event_plugins["golf"] == []
    eng.bus_for("mercedes")
    assert eng.loaded_event_plugins["mercedes"] == ["reallocate"]


def test_subscriber_cannot_ask_for_a_plugin_that_is_not_allowed(tmp_path, loaded):
    cars = CARS.replace("subscription: sig123",
                        "subscription: sig123\n  event_plugins: [reallocate, public]")
    eng = Engine(_settings(tmp_path, cars, subscriber_event_plugins="public"))
    eng.bus_for("golf")
    assert eng.loaded_event_plugins["golf"] == ["public"]


def test_subscriber_cap_plugin_ignores_overrides(tmp_path, monkeypatch):
    cars = CARS.replace("subscription: sig123", "subscription: sig123\n  cap_plugin: sneaky")
    s = _settings(tmp_path, cars, private_cap_plugin_override="warn",
                  private_cap_override_numbers=SUB)
    eng = Engine(s)
    asked = []
    from rittenregistratie.plugins import registry
    real = registry.get_private_cap_plugin
    monkeypatch.setattr(registry, "get_private_cap_plugin",
                        lambda name: asked.append(name) or real("warn"))
    eng._cap_for_car(eng.cars.get("golf"))
    assert asked == ["warn"]  # the subscriber plugin, not 'sneaky' or the override


def test_subscriber_has_its_own_places(tmp_path):
    eng = Engine(_settings(tmp_path, CARS))
    now = datetime(2026, 11, 1, 9, 0)
    # The operator's "home" is not the subscriber's: the bot asks for an address.
    reply = eng.handle_text("5040 home", SUB, now=now)
    assert "don't have an address" in str(reply)
    reply = eng.handle_text("Laan 5, Utrecht", SUB, now=now)
    assert "Laan 5, Utrecht" in str(reply)
    # Learned into the subscriber's own book, not the shared one.
    assert (tmp_path / "data" / "locations-learned-golf.yaml").exists()
    assert not (tmp_path / "data" / "locations-learned.yaml").exists()
    assert eng.book(eng.cars.get("mercedes")).address_for("home") == "Buitenhof 128, Almere"
    # And the operator's own trips still resolve the shared places.
    eng.handle_text("150 office", OWNER, now=now)
    ledger = (tmp_path / "data" / "raw-ledger-mercedes.jsonl").read_text()
    assert "Keizersgracht 1" in ledger


def test_subscriber_learned_place_resolves_next_time(tmp_path):
    eng = Engine(_settings(tmp_path, CARS))
    now = datetime(2026, 11, 1, 9, 0)
    eng.handle_text("5040 home", SUB, now=now)
    eng.handle_text("Laan 5, Utrecht", SUB, now=now)
    reply = eng.handle_text("5080 home", SUB, now=now)
    assert "don't have an address" not in str(reply)
    rows = (tmp_path / "data" / "raw-ledger-golf.jsonl").read_text().splitlines()
    assert len(rows) == 2 and "Laan 5, Utrecht" in rows[1]


def test_help_greets_a_registered_driver(tmp_path):
    eng = Engine(_settings(tmp_path, CARS))
    assert "Golf" in str(eng.handle_user_command("start", SUB))
