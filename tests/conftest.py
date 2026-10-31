import pytest

from zundamon_jarvis import i18n

_language = i18n.language


@pytest.fixture(autouse=True)
def _display_language_ja(monkeypatch):
    """The display language is Japanese in tests unless a test passes ``lang`` (or a config
    path to ``i18n.language``): neither the developer's OS nor their config.toml decides it."""
    monkeypatch.setattr(i18n, "language", lambda config_path=None: _language(config_path) if config_path else "ja")
