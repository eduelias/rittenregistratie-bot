"""Cookieless page counts, the subscription funnel, and the owner's report."""
from datetime import date, timedelta

import pytest

from rittenregistratie import analytics
from rittenregistratie.analytics import PageCounter, funnel, referrer_domain, report
from rittenregistratie.billing import ACTIVE, CANCELED, FAILED, PAUSED, Signup, SignupStore
from rittenregistratie.config import Settings

TODAY = date(2026, 12, 1)
UA = "Mozilla/5.0 (iPhone) Safari/604.1"


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config",
                    public_base_url="https://rrbot.nl")


def test_referrer_domain():
    assert referrer_domain("https://www.google.com/search?q=x", "rrbot.nl") == "google.com"
    assert referrer_domain("https://rrbot.nl/privacy", "rrbot.nl") == ""
    assert referrer_domain("https://www.rrbot.nl/", "rrbot.nl") == ""
    assert referrer_domain("", "rrbot.nl") == "" and referrer_domain("not a url") == ""


def test_counts_are_daily_totals_without_identifiers(settings):
    c = PageCounter(settings.analytics_file, "rrbot.nl")
    c.view("/", user_agent=UA, referer="https://www.linkedin.com/feed", today=TODAY,
           query="utm_source=LinkedIn&utm_campaign=Launch%20Nov")
    c.view("/", user_agent=UA, referer="https://rrbot.nl/voorwaarden", today=TODAY)
    c.view("/privacy", user_agent=UA, today=TODAY)
    c.view("/", user_agent="WhatsApp/2.23 A", today=TODAY)      # link preview fetcher
    c.view("/", user_agent="", today=TODAY)
    c.form(today=TODAY)
    raw = settings.analytics_file.read_text()
    assert "Mozilla" not in raw and "127.0.0.1" not in raw
    t = c.totals(TODAY, TODAY)
    assert t["views"] == {"/": 2, "/privacy": 1}
    assert t["referrers"] == {"linkedin.com": 1}
    assert t["sources"] == {"linkedin": 1} and t["campaigns"] == {"launch-nov": 1}
    assert t["forms"] == 1 and t["bots"] == 2


def test_old_days_are_dropped(settings):
    c = PageCounter(settings.analytics_file)
    c.view("/", user_agent=UA, today=TODAY - timedelta(days=500))
    c.view("/", user_agent=UA, today=TODAY)
    assert list(c._load()) == [TODAY.isoformat()]


def _signup(sid, created, status, plan="monthly", trial_until="", events=(), sub="sub_1"):
    return Signup(id=sid, name="X", email="x@y.nl", phone="316" + sid.zfill(8), car_label="C",
                  seed_odometer=1, seed_address="A", plan=plan,
                  created_at=f"{created.isoformat()}T10:00:00", status=status,
                  subscription_id=sub, trial_until=trial_until, events=list(events))


def test_funnel_and_mrr(settings):
    week = TODAY - timedelta(days=6)
    signups = [
        _signup("1", TODAY, ACTIVE, trial_until=(TODAY + timedelta(days=13)).isoformat()),
        _signup("2", TODAY, FAILED, sub=""),
        _signup("3", TODAY - timedelta(days=20), ACTIVE, plan="yearly",
                trial_until=(TODAY - timedelta(days=6)).isoformat(),
                events=[f"{(TODAY - timedelta(days=5)).isoformat()}T09:00:00 recurring tr_9 paid"]),
        _signup("4", TODAY - timedelta(days=30), PAUSED,
                events=[f"{(TODAY - timedelta(days=1)).isoformat()}T09:00:00 paused by customer, paid until x"]),
        _signup("5", TODAY - timedelta(days=40), CANCELED,
                events=[f"{TODAY.isoformat()}T08:00:00 cancelled by customer, access until y"]),
    ]
    f = funnel(signups, settings, week, TODAY, TODAY)
    assert (f["started"], f["paid_first"], f["failed"]) == (2, 1, 1)
    assert (f["trials_ended"], f["trials_converted"]) == (1, 1)
    assert (f["paused"], f["cancelled"], f["paused_now"]) == (1, 1, 1)
    assert (f["active"], f["in_trial"], f["monthly"], f["yearly"]) == (2, 1, 1, 1)
    assert f["mrr_paying"] == round(49 / 12, 2)
    assert f["mrr_incl_trial"] == round(49 / 12 + 4.95, 2)


def test_report_text(settings):
    c = PageCounter(settings.analytics_file, "rrbot.nl")
    for _ in range(10):
        c.view("/", user_agent=UA, referer="https://google.com/", today=TODAY)
    c.form(today=TODAY)
    SignupStore(settings.signups_file).save(
        _signup("1", TODAY, ACTIVE, trial_until=(TODAY + timedelta(days=13)).isoformat()))
    text = report(settings, days=7, today=TODAY)
    assert "10 page views" in text and "google.com 10" in text
    assert "1 forms -> 1 to payment -> 1 mandates paid (10% of homepage views)" in text
    assert "MRR: €0.00 paying, €4.95 incl. trials" in text


def test_cli_report(settings, monkeypatch, capsys):
    import rittenregistratie.config as config
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    assert analytics.main(["report", "--days", "30"]) == 0
    assert "last 30 days" in capsys.readouterr().out
