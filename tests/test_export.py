"""On-demand export: ledger -> pre_generate -> validate -> xlsx -> post_generate."""
from pathlib import Path

import pytest
from openpyxl import load_workbook

from rittenregistratie.config import Settings
from rittenregistratie.events import EXPORT_POST_GENERATE, EXPORT_PRE_GENERATE, EventBus
from rittenregistratie.excel_writer import HEADERS, TEMPLATE_EXTRA
from rittenregistratie.export import ExportError, ExportService, validate_trips
from rittenregistratie.raw_ledger import RawLedger, RawTrip


def _trip(ts, start, end, dest="Office", private=False, sender=""):
    return RawTrip(timestamp=ts, start_odo=start, end_odo=end, start_address="Home St 1",
                   end_address=dest, destination_raw=dest.lower(), is_private=private,
                   raw_message=f"{end} {dest}", sender=sender)


@pytest.fixture
def svc(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                        whatsapp_app_secret="")
    ledger = RawLedger(settings.raw_ledger_file("car"))
    ledger.append(_trip("2025-12-30T09:00:00", 100, 140))
    ledger.append(_trip("2026-01-05T09:00:00", 140, 180))
    ledger.append(_trip("2026-01-05T18:00:00", 180, 220, dest="Home St 1", private=True))
    bus = EventBus()
    return ExportService(settings, bus), bus, settings


def _rows(path):
    return list(load_workbook(path).active.iter_rows(values_only=True))


def test_years_and_filtering(svc):
    s, _, _ = svc
    assert s.years("car") == [2025, 2026]
    assert [t["end_odo"] for t in s.trips_for_year("car", 2026)] == [180, 220]
    assert s.years("nope") == []


def test_generate_identity_without_plugins(svc):
    s, _, settings = svc
    res = s.generate("car", 2026)
    assert res.path == settings.exports_dir / "trips-car-2026.xlsx"
    assert res.rows == 2 and res.handlers == 0
    rows = _rows(res.path)
    assert list(rows[0]) == HEADERS
    assert rows[0][-2:] == ("Ritnummer", "Bestuurder")
    assert rows[1][1:3] == (140, 180) and rows[1][6] == "zakelijk"
    assert rows[2][1:3] == (180, 220) and rows[2][6] == "privé"
    # Two trips on 2026-01-05: numbered 1 and 2. No cars.yaml, so no Bestuurder.
    assert [r[8:] for r in rows[1:]] == [(1, None), (2, None)]
    # No xlsx anywhere else, ledger untouched.
    assert not list(settings.data_dir.glob("*.xlsx"))
    assert len(RawLedger(settings.raw_ledger_file("car")).read_all()) == 3


def test_generate_replaces_previous_export(svc):
    s, _, _ = svc
    s.generate("car", 2026)
    res = s.generate("car", 2026)
    assert len(_rows(res.path)) == 3  # header + 2, not appended twice


def test_pre_generate_handler_output_is_what_gets_written(svc):
    s, bus, settings = svc
    seen = {}

    def handler(payload):
        seen.update(payload)
        trips = [dict(t) for t in payload["trips"]]
        trips[1]["is_private"] = False          # reclassify
        trips.append({                           # add a row
            "timestamp": "2026-01-06T09:00:00", "start_odo": 220, "end_odo": 260,
            "start_address": "Home St 1", "end_address": "Office", "is_private": False,
        })
        return {**payload, "trips": trips}

    posts = []
    bus.on(EXPORT_PRE_GENERATE, handler)
    bus.on(EXPORT_POST_GENERATE, posts.append)
    res = s.generate("car", 2026)
    assert seen["car_id"] == "car" and seen["year"] == 2026
    assert seen["data_dir"] == str(settings.data_dir)
    rows = _rows(res.path)
    assert res.rows == 3 and res.handlers == 1
    assert rows[2][6] == "zakelijk" and rows[3][1:3] == (220, 260)
    assert posts == [{"car_id": "car", "year": 2026, "path": str(res.path), "rows": 3}]
    # The ledger is not an output of the pipeline.
    assert [t.is_private for t in RawLedger(settings.raw_ledger_file("car")).read_all()] == [False, False, True]


def test_no_trips_is_an_error(svc):
    s, _, _ = svc
    with pytest.raises(ExportError, match="No trips"):
        s.generate("car", 2019)


@pytest.mark.parametrize("bad, msg", [
    ("not a list", "not a list"),
    ([1], "not an object"),
    ([{"timestamp": "2026-01-01T00:00:00"}], "missing field"),
    ([{"timestamp": "2026-01-01T00:00:00", "start_odo": "x", "end_odo": 1,
      "start_address": "a", "end_address": "b", "is_private": False}], "integers"),
    ([{"timestamp": "2026-01-01T00:00:00", "start_odo": 10, "end_odo": 5,
      "start_address": "a", "end_address": "b", "is_private": False}], "lower than start"),
    ([{"timestamp": "yesterday", "start_odo": 1, "end_odo": 5,
      "start_address": "a", "end_address": "b", "is_private": False}], "ISO"),
    ([{"timestamp": "2026-01-01T00:00:00", "start_odo": 1, "end_odo": 5,
      "start_address": "a", "end_address": "b", "is_private": "yes"}], "is_private"),
    ([{"timestamp": "2026-01-01T00:00:00", "start_odo": 1, "end_odo": 5,
      "start_address": "", "end_address": "b", "is_private": False}], "addresses"),
    ([{"timestamp": "2026-01-01T00:00:00", "start_odo": 1, "end_odo": 50,
      "start_address": "a", "end_address": "b", "is_private": False},
      {"timestamp": "2026-01-02T00:00:00", "start_odo": 40, "end_odo": 60,
      "start_address": "a", "end_address": "b", "is_private": False}], "chain broken"),
])
def test_validate_trips_rejects_bad_rows(bad, msg):
    with pytest.raises(ExportError, match=msg):
        validate_trips(bad)


def test_validate_sorts_and_allows_gaps():
    rows = validate_trips([
        {"timestamp": "2026-01-02T00:00:00", "start_odo": 60, "end_odo": 70,
         "start_address": "a", "end_address": "b", "is_private": False},
        {"timestamp": "2026-01-01T00:00:00", "start_odo": 1, "end_odo": 50,
         "start_address": "a", "end_address": "b", "is_private": True},
    ])
    assert [r["start_odo"] for r in rows] == [1, 60]


def test_bad_handler_payload_is_rejected(svc):
    s, bus, _ = svc
    bus.on(EXPORT_PRE_GENERATE, lambda p: ["oops"])
    with pytest.raises(ExportError, match="invalid payload"):
        s.generate("car", 2026)
    bus2 = EventBus()
    bus2.on(EXPORT_PRE_GENERATE, lambda p: {**p, "trips": []})
    with pytest.raises(ExportError, match="no trips"):
        ExportService(s.settings, bus2).generate("car", 2026)


def test_header_ends_with_the_template_extra_columns():
    assert HEADERS[-len(TEMPLATE_EXTRA):] == list(TEMPLATE_EXTRA) == ["Ritnummer", "Bestuurder"]


def _shared_car_svc(tmp_path, cars_yaml):
    settings = Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                        whatsapp_app_secret="")
    settings.config_dir.mkdir(parents=True)
    settings.cars_file.write_text(cars_yaml)
    ledger = RawLedger(settings.raw_ledger_file("car"))
    ledger.append(_trip("2026-03-01T08:00:00", 100, 140))                          # before senders
    ledger.append(_trip("2026-03-01T12:00:00", 140, 180, sender="31611111111"))
    ledger.append(_trip("2026-03-01T17:30:00", 180, 220, sender="31622222222"))
    ledger.append(_trip("2026-03-02T08:00:00", 220, 260, sender="31622222222"))
    return ExportService(settings, EventBus())


def test_ritnummer_per_day_and_bestuurder_for_a_shared_car(tmp_path):
    s = _shared_car_svc(tmp_path, (
        "car:\n"
        "  label: Shared\n"
        "  phones: ['31611111111', '+31 6 2222 2222']\n"
        "  drivers: {'+31611111111': 'Alex'}\n"
    ))
    rows = _rows(s.generate("car", 2026).path)
    assert [r[0] for r in rows[1:]] == ["2026-03-01"] * 3 + ["2026-03-02"]
    assert [r[8] for r in rows[1:]] == [1, 2, 3, 1]
    # Named driver, unnamed driver (the number), and nothing for the row
    # logged before the ledger recorded who sent it.
    assert [r[9] for r in rows[1:]] == [None, "Alex", "31622222222", "31622222222"]


def test_no_bestuurder_when_one_phone_drives_the_car(tmp_path):
    s = _shared_car_svc(tmp_path, "car:\n  label: Solo\n  phones: ['31611111111']\n")
    rows = _rows(s.generate("car", 2026).path)
    assert [r[8] for r in rows[1:]] == [1, 2, 3, 1]
    assert {r[9] for r in rows[1:]} == {None}


def test_ritnummer_follows_the_order_a_plugin_returns(tmp_path):
    """Rows added by a pre_generate plugin are numbered where they sort in."""
    s = _shared_car_svc(tmp_path, "car:\n  phones: ['31611111111', '31622222222']\n")

    def add_morning_trip(payload):
        trips = [dict(t) for t in payload["trips"]]
        for t in trips:
            t["start_odo"] += 10
            t["end_odo"] += 10
        trips.append({"timestamp": "2026-03-01T07:00:00", "start_odo": 100, "end_odo": 110,
                      "start_address": "a", "end_address": "b", "is_private": False})
        return {**payload, "trips": trips}

    s.bus_for("car").on(EXPORT_PRE_GENERATE, add_morning_trip)
    rows = _rows(s.generate("car", 2026).path)
    assert [(r[1], r[8]) for r in rows[1:]] == [(100, 1), (110, 2), (150, 3), (190, 4), (230, 1)]
    assert rows[1][9] is None  # the plugin's row has no sender
