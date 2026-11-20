"""The trip record is append-only and its readings chain. These tests break
things on purpose (a crash, a typo, the car and the driver reporting at once,
any text at all) and check the record stays sound."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings as hsettings, strategies as st

from rittenregistratie.cars import slugify_car_id
from rittenregistratie.config import Settings
from rittenregistratie.engine import Engine, EngineError
from rittenregistratie.export import ExportError
from rittenregistratie.models import VehicleTripReport
from rittenregistratie.parser import ParseError, parse_message
from rittenregistratie.state import State

SEED = 145000
PHONE = "31612345678"
ADMIN = "31600000001"
NOW = datetime(2026, 1, 5, 9, 0)


def _engine(tmp: Path, cars_extra: str = "", **kw) -> Engine:
    (tmp / "config").mkdir(parents=True)
    (tmp / "config" / "locations.yaml").write_text(
        "Home: {address: 'A St, Utrecht'}\nOffice: {address: 'B St, Amsterdam'}\n"
        "Gym: {address: 'C St, Utrecht', private: true}\n"
    )
    (tmp / "config" / "routes.yaml").write_text("{}\n")
    (tmp / "config" / "cars.yaml").write_text(
        f"car:\n  label: Car\n  seed_address: Home\n  seed_odometer: {SEED}\n"
        f"  phones: ['{PHONE}']\n{cars_extra}"
    )
    return Engine(Settings(
        data_dir=tmp / "data", config_dir=tmp / "config", seed_address="Home",
        seed_odometer=SEED, trajectory_provider="maps_link", private_cap_plugin="warn",
        whatsapp_app_secret="", admin_numbers=ADMIN, **kw,
    ))


def _ledger(tmp: Path, car: str = "car") -> list:
    path = tmp / "data" / f"raw-ledger-{car}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _state(tmp: Path) -> dict:
    return json.loads((tmp / "data" / "state-car.json").read_text())


# ---- the car and the driver report the same drive

def test_address_for_a_drive_the_car_already_logged_adds_no_row(tmp_path):
    eng = _engine(tmp_path)
    eng.handle_text(f"{SEED + 50} Klant X", PHONE, now=NOW)
    assert _state(tmp_path)["pending"]["end_odo"] == SEED + 50      # waiting for the address
    eng.handle_vehicle_trip(eng.cars.get("car"), VehicleTripReport(end_odo=SEED + 100, zone="office"))
    reply = eng.handle_text("Main St 1, Utrecht", PHONE, now=NOW)
    assert "Already logged" in str(reply)
    rows = _ledger(tmp_path)
    assert [(r["start_odo"], r["end_odo"]) for r in rows] == [(SEED, SEED + 100)]
    assert _state(tmp_path)["last_odometer"] == SEED + 100 and _state(tmp_path)["pending"] is None


# ---- a typo must not lock the car

def test_a_reading_far_above_the_last_is_logged_only_when_sent_twice(tmp_path):
    eng = _engine(tmp_path)
    typo = f"{SEED * 10 + 40} Office"          # one digit too many
    reply = str(eng.handle_text(typo, PHONE, now=NOW))
    assert "Nothing logged" in reply and str(SEED * 9 + 40) in reply
    assert _ledger(tmp_path) == []
    eng.handle_text(f"{SEED + 40} Office", PHONE, now=NOW)     # the right reading goes straight in
    assert [r["end_odo"] for r in _ledger(tmp_path)] == [SEED + 40]
    far = f"{SEED + 3040} Home"                # a real long gap: confirmed by sending it again
    assert "Nothing logged" in str(eng.handle_text(far, PHONE, now=NOW))
    assert eng.handle_text(far, PHONE, now=NOW).logged
    assert [r["end_odo"] for r in _ledger(tmp_path)] == [SEED + 40, SEED + 3040]
    assert _state(tmp_path)["unconfirmed_odo"] is None


def test_readings_no_car_can_have_are_refused(tmp_path):
    eng = _engine(tmp_path)
    with pytest.raises(EngineError, match="not an odometer"):
        eng.handle_text("99999999 Office", PHONE, now=NOW)
    assert eng.handle_vehicle_trip(eng.cars.get("car"), VehicleTripReport(end_odo=99999999)) is None
    assert _ledger(tmp_path) == []
    eng.handle_text(f"{SEED + 40} Office", PHONE, now=NOW)      # and the car is not locked
    assert len(_ledger(tmp_path)) == 1


# ---- a crash between the ledger and the state

def test_state_behind_the_ledger_catches_up_instead_of_logging_twice(tmp_path):
    eng = _engine(tmp_path)
    eng.handle_text(f"{SEED + 10} Office", PHONE, now=NOW)
    before = (tmp_path / "data" / "state-car.json").read_text()
    eng.handle_text(f"{SEED + 50} Home private", PHONE, now=NOW)
    (tmp_path / "data" / "state-car.json").write_text(before)   # the state save never happened
    with pytest.raises(EngineError, match="Already logged"):               # the driver sends it again
        eng.handle_text(f"{SEED + 50} Home private", PHONE, now=NOW)
    assert [(r["start_odo"], r["end_odo"]) for r in _ledger(tmp_path)] == [
        (SEED, SEED + 10), (SEED + 10, SEED + 50)]
    state = _state(tmp_path)
    assert (state["last_odometer"], state["last_address"]) == (SEED + 50, "Home")
    assert state["private_km"] == {"2026": 40}


def test_a_failing_cap_plugin_does_not_cost_the_trip(tmp_path, monkeypatch):
    eng = _engine(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("plugin bug")

    monkeypatch.setattr(eng.cap_plugin, "on_evaluate", boom)
    assert eng.handle_text(f"{SEED + 10} Office", PHONE, now=NOW).logged
    assert _state(tmp_path)["last_odometer"] == SEED + 10
    eng.handle_text(f"{SEED + 30} Home", PHONE, now=NOW)
    assert [(r["start_odo"], r["end_odo"]) for r in _ledger(tmp_path)] == [
        (SEED, SEED + 10), (SEED + 10, SEED + 30)]


# ---- small rules nothing tested

def test_a_place_marked_private_makes_a_typed_trip_private(tmp_path):
    eng = _engine(tmp_path)
    eng.handle_text(f"{SEED + 7} Gym", PHONE, now=NOW)
    assert _ledger(tmp_path)[0]["is_private"] is True
    assert _state(tmp_path)["private_km"] == {"2026": 7}


def test_private_km_are_counted_per_year():
    state = State()
    state.add_private(2025, 30)
    state.add_private(2026, 12)
    assert (state.private_ytd(2025), state.private_ytd(2026), state.private_ytd(2027)) == (30, 12, 0)


def test_private_km_start_again_on_1_january(tmp_path):
    eng = _engine(tmp_path)
    eng.handle_text(f"{SEED + 30} Home private", PHONE, now=datetime(2025, 12, 31, 23, 50))
    eng.handle_text(f"{SEED + 42} Home private -- x", PHONE, now=datetime(2026, 1, 1, 0, 10))
    assert _state(tmp_path)["private_km"] == {"2025": 30, "2026": 12}


def test_a_car_id_is_safe_as_a_file_name():
    assert slugify_car_id("../../Etc/Passwd", []) == "etc_passwd"
    assert slugify_car_id("Tesla 🚗 Model-Y", ["tesla_model_y"]) == "tesla_model_y_2"
    assert slugify_car_id("///", []) == "car"


def test_approve_never_reuses_an_id_that_still_has_trips(tmp_path):
    eng = _engine(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "raw-ledger-polo.jsonl").write_text("")   # a removed car's record
    eng.handle_admin_command("approve", ["31699999999", "Polo", "500", "Elsewhere"])
    assert "polo_2" in {c.car_id for c in eng.cars.all()}
    assert "polo" not in {c.car_id for c in eng.cars.all()}


def test_export_is_refused_when_a_named_plugin_is_not_installed(tmp_path):
    eng = _engine(tmp_path, cars_extra="  event_plugins: [not_installed]\n")
    eng.handle_text(f"{SEED + 10} Office", PHONE, now=NOW)
    with pytest.raises(ExportError, match="not_installed"):
        eng.exporter.generate("car", 2026)


def test_a_subscriber_gets_the_plain_cap_plugin_even_when_the_setting_is_blank(tmp_path):
    eng = _engine(tmp_path, cars_extra="  subscription: sub_1\n", subscriber_cap_plugin="")
    eng.cap_plugin_override = object()         # the operator's own plugin
    eng.settings.private_cap_override_numbers = PHONE
    car = eng.cars.get("car")
    assert car.is_subscriber
    assert type(eng._cap_for_car(car)).__module__.endswith("privatecap_warn")


# ---- any input at all

@given(st.text(max_size=200))
@example("² office")
@example("١٢٣ office")
@example("23250 non-private")
def test_parser_returns_a_trip_or_a_parse_error(text):
    try:
        parsed = parse_message(text)
    except ParseError:
        return
    assert parsed.end_odo >= 0
    assert parsed.destination.strip()
    assert "*" not in parsed.destination.split()


def _check_record(tmp: Path, before: list) -> list:
    rows = _ledger(tmp)
    assert rows[:len(before)] == before, "an existing row changed or disappeared"
    last = SEED
    for row in rows:
        assert row["start_odo"] == last, "readings do not chain"
        assert row["end_odo"] > row["start_odo"], "a trip of zero or negative km"
        last = row["end_odo"]
    if (tmp / "data" / "state-car.json").exists():
        assert _state(tmp)["last_odometer"] == last
    return rows


_odo = st.integers(min_value=SEED - 50, max_value=SEED + 2500).map(str)
_place = st.sampled_from(["Home", "Office", "Gym", "Klant X", "private", "privé", "*", ""])
_message = st.one_of(
    st.tuples(_odo, _place, st.sampled_from(["", " private", " -- note", " | 99999 x"])).map(" ".join),
    st.sampled_from(["cancel", "Home", "Main St 1, Utrecht", "name Depot", "ok"]),
    st.text(max_size=30),
)
_report = st.builds(
    VehicleTripReport,
    end_odo=st.integers(min_value=SEED - 50, max_value=SEED + 2500),
    start_odo=st.none() | st.integers(min_value=SEED - 50, max_value=SEED + 2500),
    zone=st.sampled_from(["", "home", "office", "elsewhere"]),
    source=st.just("test"),
)


@hsettings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(st.one_of(_report, _message, _message), max_size=10))
@example(steps=[f"{SEED + 50} Klant X", VehicleTripReport(end_odo=SEED + 100), "Main St 1, Utrecht"])
@example(steps=[f"{SEED + 50} Klant X", VehicleTripReport(end_odo=SEED + 50), "Home"])
def test_no_sequence_of_messages_and_car_trips_breaks_the_record(tmp_path_factory, steps):
    tmp = tmp_path_factory.mktemp("prop")
    eng = _engine(tmp)
    car = eng.cars.get("car")
    rows, now = [], NOW
    for step in steps:
        now += timedelta(minutes=30)
        try:
            if isinstance(step, str):
                eng.handle_text(step, PHONE, now=now)
            else:
                eng.handle_vehicle_trip(car, step)
        except (ParseError, EngineError):
            pass
        rows = _check_record(tmp, rows)
    if rows:
        eng.exporter.generate("car", 2026)     # and what was recorded still exports
