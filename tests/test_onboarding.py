"""Onboarding flow tests: join requests, admin approve/deny/pending."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from rittenregistratie.config import Settings
from rittenregistratie.engine import Engine


@pytest.fixture
def settings(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "locations.yaml").write_text("Home: {address: 'A St'}\n")
    (cfg / "routes.yaml").write_text("{}\n")
    (cfg / "cars.yaml").write_text(
        "mycar:\n  label: Mine\n  seed_address: Home\n  seed_odometer: 100\n"
        "  phones: ['31600000001']\n"
    )
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=cfg,
        admin_numbers="31600000001",
        trajectory_provider="maps_link",
        
        private_cap_plugin="warn",
        whatsapp_app_secret="",
    )


def test_admin_detection(settings):
    eng = Engine(settings)
    assert eng.is_admin("31600000001")
    assert eng.is_admin("+316 000 00001")  # normalized
    assert not eng.is_admin("31699999999")


def test_join_request_and_pending(settings):
    eng = Engine(settings)
    assert eng.register_join_request("31612345678", "hello")
    assert not eng.register_join_request("31612345678", "again")  # dupe
    reply = eng.handle_admin_command("pending", [])
    assert "31612345678" in reply


def test_approve_registers_car(settings):
    eng = Engine(settings)
    eng.register_join_request("31612345678", "hi")
    reply = eng.handle_admin_command(
        "approve", ["31612345678", "Alice", "Golf", "45000", "Delft"]
    )
    assert reply.startswith("Approved")
    # now resolvable
    car = eng.cars.resolve("31612345678")
    assert car is not None
    assert car.seed_odometer == 45000
    assert car.seed_address == "Delft"
    assert "Alice Golf" == car.label
    # request cleared
    assert not eng.onboarding.has("31612345678")


def test_deny_removes_request(settings):
    eng = Engine(settings)
    eng.register_join_request("31612345678", "hi")
    reply = eng.handle_admin_command("deny", ["31612345678"])
    assert "Denied" in reply
    assert not eng.onboarding.has("31612345678")


def test_approve_requires_odometer(settings):
    eng = Engine(settings)
    reply = eng.handle_admin_command("approve", ["31612345678", "Alice"])
    assert "Usage" in reply or "seed odometer" in reply.lower()


def test_approve_rejects_duplicate_phone(settings):
    eng = Engine(settings)
    reply = eng.handle_admin_command(
        "approve", ["31600000001", "Dup", "500"]
    )
    assert "already registered" in reply.lower()


def test_list_users(settings):
    eng = Engine(settings)
    reply = eng.handle_admin_command("list", [])
    assert "Mine" in reply
    assert "1/5" in reply


def test_remove_user(settings):
    eng = Engine(settings)
    eng.handle_admin_command("approve", ["31612345678", "Alice", "45000"])
    assert eng.cars.resolve("31612345678") is not None
    reply = eng.handle_admin_command("remove", ["31612345678"])
    assert "Removed" in reply
    assert eng.cars.resolve("31612345678") is None


def test_remove_by_car_id(settings):
    eng = Engine(settings)
    reply = eng.handle_admin_command("remove", ["mycar"])
    assert "Removed" in reply
    assert eng.cars.resolve("31600000001") is None


def test_remove_unknown(settings):
    eng = Engine(settings)
    reply = eng.handle_admin_command("remove", ["31699999999"])
    assert "No user found" in reply


def test_max_users_enforced(settings):
    settings.max_users = 2  # mycar already exists (1)
    eng = Engine(settings)
    r1 = eng.handle_admin_command("approve", ["31600000002", "Bob", "100"])
    assert r1.startswith("Approved")
    r2 = eng.handle_admin_command("approve", ["31600000003", "Carol", "100"])
    assert "limit reached" in r2.lower()


def test_cap_override_only_for_allowlisted(settings, tmp_path):
    # Register two cars: one allow-listed for the override, one not.
    (settings.config_dir / "cars.yaml").write_text(
        "vip:\n  label: VIP\n  seed_address: Home\n  seed_odometer: 100\n"
        "  phones: ['31600000001']\n"
        "other:\n  label: Other\n  seed_address: Home\n  seed_odometer: 100\n"
        "  phones: ['31600000002']\n"
    )
    settings.private_cap_plugin = "warn"
    settings.private_cap_plugin_override = "warn"  # any registered plugin
    settings.private_cap_override_numbers = "31600000001"
    eng = Engine(settings)

    vip = eng.cars.resolve("31600000001")
    other = eng.cars.resolve("31600000002")
    # VIP gets the override instance; other gets the default instance
    assert eng._cap_for_car(vip) is eng.cap_plugin_override
    assert eng._cap_for_car(other) is eng.cap_plugin


def test_no_override_configured_uses_default(settings):
    eng = Engine(settings)  # no override set in fixture
    car = eng.cars.resolve("31600000001")
    assert eng._cap_for_car(car) is eng.cap_plugin
