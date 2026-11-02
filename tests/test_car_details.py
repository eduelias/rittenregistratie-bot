"""The car details the Belastingdienst asks for: number plate, make and model,
period of use. Set over WhatsApp or at signup, shown on the export's Auto sheet."""

BOT_LANGUAGE = "nl"
from datetime import date

import yaml
from openpyxl import load_workbook

from rittenregistratie.billing import Billing
from rittenregistratie.engine import Engine, parse_day
from test_billing import ADMIN, FORM, FakeMollie, _settings


def _engine(tmp_path):
    return Engine(_settings(tmp_path))


def _car(engine):
    return yaml.safe_load(engine.settings.cars_file.read_text())["mercedes"]


def test_set_show_and_clear_details(tmp_path):
    engine = _engine(tmp_path)
    shown = str(engine.handle_user_command("autogegevens", ADMIN))
    assert "Kenteken: niet opgegeven" in shown and "t/m nu" in shown
    assert "Opgeslagen" in str(engine.handle_user_command("kenteken 12 abc 3", ADMIN))
    engine.handle_user_command("merk  Mercedes-Benz   C 300e", ADMIN)
    engine.handle_user_command("in gebruik sinds 1-3-2026", ADMIN)
    reply = str(engine.handle_user_command("in gebruik tot 2026-02-01", ADMIN))
    assert "einddatum ligt voor de begindatum" in reply
    car = _car(engine)
    assert (car["plate"], car["make_model"], car["in_use_from"]) == (
        "12ABC3", "Mercedes-Benz C 300e", "2026-03-01")
    assert "in_use_until" not in car
    assert "1 maart 2026 t/m nu" in str(engine.handle_user_command("merk", ADMIN))
    engine.handle_user_command("kenteken -", ADMIN)
    assert "plate" not in _car(engine) and engine.cars.get("mercedes").plate == ""
    assert "geen kenteken" in str(engine.handle_user_command("kenteken !!!", ADMIN))
    assert "geen datum" in str(engine.handle_user_command("in gebruik sinds gisteren", ADMIN))


def test_english_commands_and_no_clash_with_trips(tmp_path):
    engine = _engine(tmp_path)
    engine.languages.set(ADMIN, "en")
    from rittenregistratie.i18n import set_current
    set_current("en")
    assert "Saved." in str(engine.handle_user_command("plate XY-123-Z", ADMIN))
    engine.handle_user_command("in use since today", ADMIN)
    assert "Number plate: XY-123-Z" in str(engine.handle_user_command("car details", ADMIN))
    assert _car(engine)["in_use_from"] == date.today().isoformat()
    assert engine.handle_user_command("45230 Merkelbeekweg 1", ADMIN) is None
    assert engine.handle_user_command("make", ADMIN) is not None
    assert parse_day("31/12/2026") == date(2026, 12, 31) and parse_day("2026-13-01") is None


def test_export_has_the_auto_sheet(tmp_path):
    engine = _engine(tmp_path)
    engine.handle_text("120 home", ADMIN)
    year = date.today().year
    path = engine.exporter.generate("mercedes", year).path
    sheet = {r[0]: r[1] for r in load_workbook(path)["Auto"].iter_rows(values_only=True)}
    assert sheet["Kenteken"].startswith("niet opgegeven; stuur 'kenteken")
    assert sheet["In gebruik tot"] == "nog in gebruik" and sheet["Jaar"] == year
    assert sheet["Ritten in dit overzicht"].startswith("1, van ")
    engine.handle_user_command("kenteken 12-ABC-3", ADMIN)
    engine.handle_user_command("merk Mercedes C", ADMIN)
    engine.handle_user_command("in gebruik sinds 1-1-2026", ADMIN)
    wb = load_workbook(engine.exporter.generate("mercedes", year).path)
    assert wb.sheetnames == ["Ritten", "Auto"] and wb.active.title == "Ritten"
    sheet = {r[0]: r[1] for r in wb["Auto"].iter_rows(values_only=True)}
    assert (sheet["Kenteken"], sheet["Merk en type"], sheet["In gebruik vanaf"]) == (
        "12-ABC-3", "Mercedes C", "01-01-2026")


async def test_signup_plate_reaches_the_car(tmp_path):
    settings = _settings(tmp_path)
    engine, mollie = Engine(settings), FakeMollie()

    async def notify(phone, text):
        return True

    billing = Billing(settings, engine, mollie, notify)
    await billing.start(dict(FORM, plate="12 abc 3", make_model="VW  Golf"))
    signup = billing.store.all()[0]
    mollie.payments[signup.first_payment_id]["status"] = "paid"
    signup = await billing.sync_payment(signup.first_payment_id)
    car = engine.cars.get(signup.car_id)
    assert (car.plate, car.make_model) == ("12ABC3", "VW Golf")
    import pytest
    from rittenregistratie.billing import SignupError
    with pytest.raises(SignupError):
        billing.validate(dict(FORM, phone="0698765432", plate="no plate!"))
