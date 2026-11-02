"""What search engines see: robots.txt, sitemap, 404, titles, structured data,
the guide pages and the free Excel template."""
import importlib
import io
import json
import re

import pytest
from openpyxl import load_workbook

from rittenregistratie.excel_writer import HEADERS
from rittenregistratie.site import DESCRIPTION, GUIDES, TITLE, company_facts

UA = {"user-agent": "Mozilla/5.0 Safari"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("locations.yaml", "routes.yaml", "cars.yaml"):
        (cfg / name).write_text("{}\n")
    for k, v in {"RIT_CONFIG_DIR": str(cfg), "RIT_DATA_DIR": str(tmp_path / "data"),
                 "RIT_WHATSAPP_APP_SECRET": "", "RIT_WHATSAPP_TOKEN": "",
                 "RIT_WHATSAPP_PHONE_NUMBER_ID": "", "RIT_MOLLIE_API_KEY": "",
                 "RIT_PUBLIC_BASE_URL": "https://rrbot.example",
                 "RIT_COMPANY_LINE": "Buitenhof 128, 1354 GT Almere · KvK 91476003 · btw NL004896802B29",
                 }.items():
        monkeypatch.setenv(k, v)
    import rittenregistratie.config as config
    config._settings = None
    import rittenregistratie.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app), main


def test_robots_and_sitemap(client):
    c = client[0]
    robots = c.get("/robots.txt")
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://rrbot.example/sitemap.xml" in robots.text
    assert "Disallow: /aanmelden" in robots.text
    sitemap = c.get("/sitemap.xml")
    assert sitemap.headers["content-type"].startswith("application/xml")
    locs = re.findall(r"<loc>(.*?)</loc>", sitemap.text)
    assert "https://rrbot.example/" in locs
    for path in GUIDES:
        assert f"https://rrbot.example{path}" in locs
    assert not any("herroeping" in loc or "opzeggen" in loc for loc in locs)


def test_unknown_page_is_a_real_html_404(client):
    c = client[0]
    r = c.get("/bestaat-niet")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Deze pagina bestaat niet" in r.text and 'content="noindex"' in r.text
    assert '<link rel="canonical"' not in r.text
    # API errors keep their JSON.
    r = c.post("/hooks/x/nocar", json={})
    assert r.status_code in (401, 403, 404) and r.headers["content-type"].startswith("application/json")


def test_titles_and_descriptions_fit_search_results(client):
    c = client[0]
    assert len(TITLE) <= 60 and len(DESCRIPTION) <= 155
    home = c.get("/").text
    assert f"<title>{TITLE}</title>" in home
    for kind in ("kilometerregistratie", "zzp", "bestelauto", "Excel", "privégebruik"):
        assert kind in home, kind
    for path, (_, title, description) in GUIDES.items():
        assert len(description) <= 155, path
        assert len(f"{title}. RittenRegistratie-bot") <= 70, path
        page = c.get(path).text
        assert f'<link rel="canonical" href="https://rrbot.example{path}">' in page
        assert 'content="index"' in page and page.count("<h1") == 1
        assert f'<meta name="description" content="{description}">' in page
        assert 'href="/#aanmelden"' in page
        assert f'href="{path}"' in home


def test_organization_structured_data(client):
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>',
                              client[0].get("/").text, re.S).group(1))
    org = ld["@graph"][0]
    assert org["address"]["postalCode"] == "1354 GT" and org["address"]["streetAddress"] == "Buitenhof 128"
    assert org["identifier"]["value"] == "91476003" and org["vatID"] == "NL004896802B29"
    assert org["sameAs"] == ["https://du7.dev"]
    assert any(n["@type"] == "WebSite" and n["name"] == "RittenRegistratie-bot" for n in ld["@graph"])
    # Placeholders are left out rather than published.
    assert company_facts("KvK [nummer] · btw [nummer]") == {}
    from rittenregistratie.site import dotted
    assert dotted("Buitenhof 128, 1354 GT Almere · KvK 91476003 · btw NL004896802B29") == \
        "Buitenhof 128, 1354 GT Almere. KvK 91476003. Btw NL004896802B29"


def test_excel_template_download(client):
    c = client[0]
    r = c.get("/rittenregistratie-excel/sjabloon.xlsx", headers=UA)
    assert r.status_code == 200
    assert 'filename="rittenregistratie-sjabloon.xlsx"' in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Ritten", "Overzicht", "Uitleg"]
    # Same columns as the bot's export, Ritnummer and Bestuurder last.
    assert [cell.value for cell in wb["Ritten"][1]] == HEADERS
    assert HEADERS[-2:] == ["Ritnummer", "Bestuurder"]
    assert {row[0].value for row in wb["Uitleg"].iter_rows(min_row=2)} >= set(HEADERS)
    assert "https://rrbot.example/rittenregistratie-eisen" in [
        row[1].value for row in wb["Uitleg"].iter_rows(min_row=2)]


def test_guides_and_downloads_are_counted(client):
    c, main = client
    for path in GUIDES:
        c.get(path, headers=UA)
    c.get("/rittenregistratie-excel/sjabloon.xlsx", headers=UA)
    data = json.loads(main._settings.analytics_file.read_text())
    views = next(iter(data.values()))["views"]
    assert set(GUIDES) | {"/rittenregistratie-excel/sjabloon.xlsx"} <= set(views)
