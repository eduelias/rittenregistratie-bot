"""Trip data of former subscribers is deleted after the retention period;
nothing else is ever touched. Plus the signup rate limit."""
import json
from datetime import date, timedelta

import pytest

from rittenregistratie import retention
from rittenregistratie.billing import ACTIVE, CANCELED, ENDED, Signup, SignupStore
from rittenregistratie.config import Settings
from rittenregistratie.site import SignupLimiter

TODAY = date(2027, 3, 1)


@pytest.fixture
def settings(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "cars.yaml").write_text(
        "mercedes:\n  label: M\n  seed_odometer: 1\n  phones: ['31600000001']\n"
        "polo:\n  label: P\n  seed_odometer: 1\n  phones: ['31600000003']\n  subscription: s3\n")
    data = tmp_path / "data"
    (data / "exports").mkdir(parents=True)
    for car in ("mercedes", "golf", "clio", "polo"):
        (data / f"raw-ledger-{car}.jsonl").write_text("{}\n")
        (data / f"state-{car}.json").write_text("{}")
        (data / "exports" / f"trips-{car}-2026.xlsx").write_text("x")
    (data / "locations-learned-golf.yaml").write_text("{}")
    (data / "active-car.json").write_text(json.dumps({"31600000002": "golf", "31600000001": "mercedes"}))
    return Settings(data_dir=data, config_dir=cfg, retention_days=90)


def _signup(settings, sid, car, phone, status, ended_days_ago):
    s = Signup(id=sid, name="X", email="x@y.nl", phone=phone, car_label=car, seed_odometer=1,
               seed_address="A", plan="monthly", created_at="2026-11-01T00:00:00",
               status=status, car_id=car,
               access_until=(TODAY - timedelta(days=ended_days_ago)).isoformat())
    SignupStore(settings.signups_file).save(s)


def test_only_ended_subscribers_past_retention_are_due(settings):
    _signup(settings, "s1", "golf", "31600000002", ENDED, 91)    # due
    _signup(settings, "s2", "clio", "31600000004", ENDED, 90)    # last day kept
    _signup(settings, "s3", "polo", "31600000003", ENDED, 400)   # back in cars.yaml
    _signup(settings, "s4", "mercedes", "31600000001", CANCELED, 400)  # not ended
    assert [p.car_id for p in retention.due(settings, TODAY)] == ["golf"]


def test_apply_deletes_the_car_files_and_marks_the_signup(settings):
    _signup(settings, "s1", "golf", "31600000002", ENDED, 120)
    purges = retention.due(settings, TODAY)
    assert {f.name for f in purges[0].files} == {
        "raw-ledger-golf.jsonl", "state-golf.json", "locations-learned-golf.yaml", "trips-golf-2026.xlsx"}
    retention.apply(settings, purges)
    data = settings.data_dir
    assert not list(data.rglob("*golf*"))
    assert (data / "raw-ledger-mercedes.jsonl").exists()          # owner untouched
    assert json.loads((data / "active-car.json").read_text()) == {"31600000001": "mercedes"}
    assert SignupStore(settings.signups_file).get("s1").data_deleted_at
    assert retention.due(settings, TODAY) == []                   # not twice


def test_a_car_id_reused_by_a_live_signup_is_kept(settings):
    _signup(settings, "s1", "golf", "31600000002", ENDED, 200)
    _signup(settings, "s5", "golf", "31600000002", ACTIVE, 0)
    assert retention.due(settings, TODAY) == []


def test_cli_dry_run_changes_nothing(settings, monkeypatch, capsys):
    # The CLI runs on today's date; end the subscription 120 days before it.
    _signup(settings, "s1", "golf", "31600000002", ENDED, 120 + (TODAY - date.today()).days)
    import rittenregistratie.config as config
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert retention.main([]) == 0
    assert "golf" in capsys.readouterr().out
    assert (settings.data_dir / "raw-ledger-golf.jsonl").exists()
    retention.main(["--apply"])
    assert not (settings.data_dir / "raw-ledger-golf.jsonl").exists()


def test_signup_limiter_per_ip_and_total():
    now = [0.0]
    lim = SignupLimiter(per_ip=2, total=3, clock=lambda: now[0])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert lim.allow("b") and not lim.allow("c")       # total of 3 reached
    now[0] = 3601
    assert lim.allow("a") and lim.allow("c")           # window passed
