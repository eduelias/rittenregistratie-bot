"""Bot language for tests.

Replies are Dutch by default (RIT_DEFAULT_LANGUAGE=nl). Most tests here
assert the English texts, so they run with English as the default; a module
that checks Dutch texts sets ``BOT_LANGUAGE = "nl"``. The per-message
language is reset between tests.
"""
import pytest

from rittenregistratie import i18n


@pytest.fixture(autouse=True)
def no_local_env(tmp_path, monkeypatch):
    """Settings reads ``.env`` from the working folder. A developer's rendered
    ``.env`` (plugins, a Mollie key, an app secret) must not decide test results."""
    monkeypatch.chdir(tmp_path)
    # Tests post unsigned webhooks; production refuses them without a secret.
    monkeypatch.setenv("RIT_WHATSAPP_ALLOW_UNSIGNED", "true")


@pytest.fixture(autouse=True)
def bot_language(request, monkeypatch):
    lang = getattr(request.module, "BOT_LANGUAGE", "en")
    monkeypatch.setenv("RIT_DEFAULT_LANGUAGE", lang)
    i18n.set_default(lang)
    i18n.set_current(None)
    yield lang
    i18n.set_current(None)
    i18n.set_default("nl")
