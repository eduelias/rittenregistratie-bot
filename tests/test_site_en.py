"""The English website: /en/ pages, hreflang pairs, the language switch, and a
signup from the English site that makes the bot speak English."""
import json
import re

import pytest

from rittenregistratie.site import EN_DESCRIPTION, EN_GUIDES, EN_TITLE, PAIRS
from test_billing import FORM, app  # noqa: F401  (fixture)
from test_site_seo import UA, client  # noqa: F401  (fixture)

BASE = "https://rrbot.example"


def _links(html):
    return dict(re.findall(r'<link rel="alternate" hreflang="([^"]+)" href="([^"]+)">', html))


def test_english_landing_page(client):
    c = client[0]
    page = c.get("/en/").text
    assert '<html lang="en">' in page
    assert f"<title>{EN_TITLE}</title>" in page and len(EN_TITLE) <= 60
    assert f'<meta name="description" content="{EN_DESCRIPTION}">' in page and len(EN_DESCRIPTION) <= 155
    assert f'<link rel="canonical" href="{BASE}/en/">' in page
    assert _links(page) == {"nl": f"{BASE}/", "en": f"{BASE}/en/", "x-default": f"{BASE}/"}
    assert '<meta property="og:locale" content="en_GB">' in page
    assert '<meta property="og:locale:alternate" content="nl_NL">' in page
    assert '<a class="lang" href="/" hreflang="nl" lang="nl">NL</a>' in page
    assert "4.95" in page and "49.00" in page and "VAT NL004896802B29" in page
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
    app_node = ld["@graph"][1]
    assert app_node["inLanguage"] == "en" and app_node["url"] == f"{BASE}/en/"
    assert [o["name"] for o in app_node["offers"]] == ["Monthly", "Yearly"]
    assert c.get("/en", follow_redirects=False).headers["location"] == "/en/"


def test_dutch_pages_point_to_english(client):
    c = client[0]
    home = c.get("/").text
    assert '<a class="lang" href="/en/" hreflang="en" lang="en">EN</a>' in home
    assert _links(home) == {"nl": f"{BASE}/", "en": f"{BASE}/en/", "x-default": f"{BASE}/"}
    assert '<meta property="og:locale" content="nl_NL">' in home
    # A Dutch page without an English one: no hreflang, the switch goes to /en/.
    excel = c.get("/rittenregistratie-excel").text
    assert not _links(excel) and 'class="lang" href="/en/"' in excel


@pytest.mark.parametrize("nl,en", list(PAIRS.items()))
def test_every_pair_links_both_ways(client, nl, en):
    c = client[0]
    expected = {"nl": BASE + nl, "en": BASE + en, "x-default": BASE + nl}
    nl_page, en_page = c.get(nl, headers=UA), c.get(en, headers=UA)
    assert nl_page.status_code == en_page.status_code == 200
    assert _links(nl_page.text) == _links(en_page.text) == expected
    assert f'<link rel="canonical" href="{BASE}{en}">' in en_page.text
    assert f'class="lang" href="{nl}"' in en_page.text and '<html lang="en">' in en_page.text
    assert "$" not in re.sub(r"<script.*?</script>", "", en_page.text, flags=re.S)  # all filled


def test_english_guide_and_legal_pages(client):
    c = client[0]
    for path, (_, title, description) in EN_GUIDES.items():
        page = c.get(path).text
        assert len(f"{title} · RittenRegistratie-bot") <= 70 and len(description) <= 155
        assert page.count("<h1") == 1 and "belastingdienst.nl" in page
        assert "not tax advice" in page.lower() and 'href="/en/#signup"' in page
    for path, original in (("/en/terms", "/voorwaarden"), ("/en/privacy", "/privacy")):
        page = c.get(path).text
        assert "Dutch version prevails" in page and f'href="{original}"' in page
    assert 'href="/herroeping"' in c.get("/en/terms").text


def test_sitemap_lists_both_languages_with_alternates(client):
    xml = client[0].get("/sitemap.xml").text
    assert 'xmlns:xhtml="http://www.w3.org/1999/xhtml"' in xml
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    for nl, en in PAIRS.items():
        assert BASE + nl in locs and BASE + en in locs
    entry = re.search(rf"<url><loc>{re.escape(BASE)}/en/terms</loc>(.*?)</url>", xml).group(1)
    assert f'hreflang="nl" href="{BASE}/voorwaarden"' in entry
    assert f'hreflang="x-default" href="{BASE}/voorwaarden"' in entry


def test_english_404_and_counts(client):
    c, main = client
    r = c.get("/en/does-not-exist")
    assert r.status_code == 404 and "This page doesn't exist" in r.text and '<html lang="en">' in r.text
    c.get("/en/", headers=UA)
    c.get("/en/terms", headers=UA)
    c.get("/", headers=UA)
    (day,) = json.loads(main._settings.analytics_file.read_text()).values()
    assert day["views"] == {"/en/": 1, "/en/terms": 1, "/": 1}
    from rittenregistratie.analytics import report
    assert "English site: 2 page views, 1 on its homepage" in report(main._settings)


# --- signup from the English site ----------------------------------------------

def test_english_signup_speaks_english(app):
    client, main, fake = app
    assert 'name="lang" value="en"' in client.get("/en/").text
    r = client.post("/aanmelden", data=dict(FORM, email="x", lang="en"))
    assert r.status_code == 400 and "Enter a valid email address." in r.text
    assert '<html lang="en">' in r.text

    client.post("/aanmelden", data=dict(FORM, lang="en"), follow_redirects=False)
    payment = [body for kind, body in fake.calls if kind == "payment"][-1]
    assert payment["locale"] == "en_US" and "mandate (monthly plan)" in payment["description"]
    signup = main._billing.store.all()[0]
    assert signup.lang == "en"
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    assert main._engine.languages.of(signup.phone) == "en"
    done = client.get(f"/aanmelden/klaar?id={signup.id}").text
    assert "Welcome, Anna!" in done and "free trial lasts 14 days" in done


def test_dutch_signup_is_unchanged(app):
    client, main, fake = app
    client.post("/aanmelden", data=FORM, follow_redirects=False)
    payment = [body for kind, body in fake.calls if kind == "payment"][-1]
    assert payment["locale"] == "nl_NL" and "machtiging (maandelijks abonnement)" in payment["description"]
    signup = main._billing.store.all()[0]
    fake.payments[signup.first_payment_id]["status"] = "paid"
    client.post("/billing/mollie/webhook", data={"id": signup.first_payment_id})
    assert main._engine.languages.get(signup.phone) is None   # the default (Dutch)
    assert "Welkom, Anna!" in client.get(f"/aanmelden/klaar?id={signup.id}").text
