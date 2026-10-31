from zundamon_jarvis import i18n
from zundamon_jarvis.config import load_config

TABLE = {"hello": {"ja": "こんにちは {name}", "en": "Hello {name}"}, "only_ja": {"ja": "日本語だけ"}}


def test_text_picks_the_language_and_formats():
    assert i18n.text(TABLE, "hello", "en", name="Taro") == "Hello Taro"
    assert i18n.text(TABLE, "hello", "ja", name="太郎") == "こんにちは 太郎"


def test_missing_language_falls_back_to_japanese():
    assert i18n.text(TABLE, "only_ja", "en") == "日本語だけ"


def test_system_language_from_the_environment():
    assert i18n.system_language({"LANG": "ja_JP.UTF-8"}, "linux") == "ja"
    assert i18n.system_language({"LANG": "en_US.UTF-8"}, "linux") == "en"
    assert i18n.system_language({"LC_ALL": "de_DE.UTF-8", "LANG": "ja_JP.UTF-8"}, "linux") == "en"


def test_configured_language_wins(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('language = "en"\n', encoding="utf-8")
    assert load_config(cfg).language == "en"
    assert i18n.language(cfg) == "en"
    cfg.write_text('language = "fr"\n', encoding="utf-8")  # unknown: follow the OS
    assert load_config(cfg).language is None
    assert i18n.resolve("ja") == "ja"


def test_mac_language_from_defaults():
    class R:
        stdout = '(\n    "en-JP",\n    "ja-JP"\n)\n'

    assert i18n._mac_ui_language(lambda *a, **k: R()) == "en"
    R.stdout = '(\n    "ja-JP"\n)\n'
    assert i18n._mac_ui_language(lambda *a, **k: R()) == "ja"
