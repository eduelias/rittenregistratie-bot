"""Bot language for tests.

Replies are Dutch by default (RIT_DEFAULT_LANGUAGE=nl). Most tests here
assert the English texts, so they run with English as the default; a module
that checks Dutch texts sets ``BOT_LANGUAGE = "nl"``. The per-message
language is reset between tests.
"""
import pytest

from rittenregistratie import i18n


@pytest.fixture(autouse=True)
def bot_language(request, monkeypatch):
    lang = getattr(request.module, "BOT_LANGUAGE", "en")
    monkeypatch.setenv("RIT_DEFAULT_LANGUAGE", lang)
    i18n.set_default(lang)
    i18n.set_current(None)
    yield lang
    i18n.set_current(None)
    i18n.set_default("nl")
