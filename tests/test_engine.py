from datetime import datetime
from pathlib import Path

import pytest

from rittenregistratie.config import Settings
from rittenregistratie.engine import Engine, EngineError
from openpyxl import load_workbook


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "locations.yaml").write_text(
        "Home: {address: 'A St, Utrecht'}\nOffice: {address: 'B St, Amsterdam'}\n"
    )
    (tmp_path / "config" / "routes.yaml").write_text(
        "Home->Office: {expected_km: 40, variants: [38, 40, 43]}\n"
    )
    (tmp_path / "config" / "cars.yaml").write_text(
        "default_car:\n"
        "  label: 'Test car'\n"
        "  seed_address: Home\n"
        "  seed_odometer: 145000\n"
        "  phones: ['31612345678']\n"
    )
    return Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        seed_address="Home",
        seed_odometer=145000,
        trajectory_provider="maps_link",
        
        private_cap_plugin="warn",
        whatsapp_app_secret="",
    )


def test_first_trip_uses_seed(settings):
    eng = Engine(settings)
    reply = eng.handle_text("145040 Office", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    assert "40 km" in reply
    assert "business" in reply
    wb = load_workbook(settings.data_dir / "trips-default_car-2026.xlsx")
    rows = list(wb.active.iter_rows(values_only=True))
    assert rows[0][0] == "Date"
    assert rows[1][5] == 145000  # StartOdo == seed
    assert rows[1][6] == 145040  # EndOdo


def test_deviation_detected(settings):
    eng = Engine(settings)
    reply = eng.handle_text("145050 Office", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    assert "expected" in reply.lower()


def test_monotonic_enforced(settings):
    eng = Engine(settings)
    eng.handle_text("145040 Office", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    with pytest.raises(EngineError):
        eng.handle_text("145030 Home", "31612345678", now=datetime(2026, 1, 5, 18, 0))


def test_private_cap_warning(settings):
    eng = Engine(settings)
    # 600 km private trip exceeds the 500 km cap.
    reply = eng.handle_text("145600 Beach private", "31612345678", now=datetime(2026, 6, 1, 9, 0))
    assert "exceeds" in reply.lower()


def test_unknown_number_rejected(settings):
    from rittenregistratie.engine import UnknownCarError
    eng = Engine(settings)
    with pytest.raises(UnknownCarError):
        eng.handle_text("145040 Office", "31600000000", now=datetime(2026, 1, 5, 9, 0))


def test_unknown_destination_prompts_for_address(settings):
    eng = Engine(settings)
    reply = eng.handle_text("145050 Gym", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    assert "don't have an address" in reply.lower()
    # No trip written yet
    from pathlib import Path
    assert not (settings.data_dir / "trips-default_car-2026.xlsx").exists()


def test_unknown_destination_resolved_by_text(settings):
    eng = Engine(settings)
    eng.handle_text("145050 Gym", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    reply = eng.handle_text("Sportlaan 1, Almere", "31612345678", now=datetime(2026, 1, 5, 9, 5))
    assert "Sportlaan 1" in reply
    wb = load_workbook(settings.data_dir / "trips-default_car-2026.xlsx")
    rows = list(wb.active.iter_rows(values_only=True))
    assert rows[-1][4] == "Sportlaan 1, Almere"  # EndAddress
    # location learned for next time
    reply2 = eng.handle_text("145090 Gym", "31612345678", now=datetime(2026, 1, 6, 9, 0))
    assert "don't have an address" not in reply2.lower()


def test_unknown_destination_resolved_by_location(settings):
    eng = Engine(settings)
    eng.handle_text("145050 Gym", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    reply = eng.handle_location(
        "31612345678", 52.37, 4.90, address="Damrak 1, Amsterdam",
        now=datetime(2026, 1, 5, 9, 5),
    )
    assert "Damrak 1" in reply


def test_private_unknown_destination_not_prompted(settings):
    eng = Engine(settings)
    reply = eng.handle_text("145030 beach private", "31612345678", now=datetime(2026, 1, 5, 9, 0))
    assert "don't have an address" not in reply.lower()
    assert "private" in reply.lower()
