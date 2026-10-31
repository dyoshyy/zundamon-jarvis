import json
from pathlib import Path

from zundamon_jarvis.config import load_config


def test_mic_device_defaults_to_the_os_default_input(tmp_path):
    assert load_config(tmp_path / "missing.toml").mic_device is None
    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\n", encoding="utf-8")
    assert load_config(p).mic_device is None


def test_mic_device_can_switch_to_aec_source(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('mic_device = "zundamon_jarvis_aec_source"\n', encoding="utf-8")
    assert load_config(p).mic_device == "zundamon_jarvis_aec_source"


def test_model_defaults_to_sonnet(tmp_path):
    assert load_config(tmp_path / "missing.toml").model == "sonnet"
    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\n", encoding="utf-8")
    assert load_config(p).model == "sonnet"


def test_model_can_be_overridden_or_inherited(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('model = "opus"\n', encoding="utf-8")
    assert load_config(p).model == "opus"
    p.write_text('model = ""\n', encoding="utf-8")  # empty: inherit the CLI default
    assert load_config(p).model is None


# ---- speed ----

def test_speed_defaults_to_1_2(tmp_path):
    from zundamon_jarvis.config import DEFAULT_SPEED

    assert DEFAULT_SPEED == 1.2
    assert load_config(tmp_path / "missing.toml").speed == 1.2
    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\n", encoding="utf-8")
    assert load_config(p).speed == 1.2


def test_speed_is_read_and_out_of_range_falls_back(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("speed = 1.5\n", encoding="utf-8")
    assert load_config(p).speed == 1.5
    p.write_text("speed = 1\n", encoding="utf-8")  # an int is fine
    assert load_config(p).speed == 1.0
    for bad in ("speed = 9.0", "speed = 0.1", 'speed = "fast"', "speed = true"):
        p.write_text(bad + "\n", encoding="utf-8")
        assert load_config(p).speed == 1.2, bad


# ---- write-back ----

def test_update_creates_the_file(tmp_path):
    from zundamon_jarvis.config import update_config

    p = tmp_path / "zundamon-jarvis" / "config.toml"
    update_config({"speed": 1.3, "speaker_id": 2, "model": "opus"}, p)
    cfg = load_config(p)
    assert (cfg.speed, cfg.speaker_id, cfg.model) == (1.3, 2, "opus")


def test_update_keeps_other_and_unknown_keys_comments_and_tables(tmp_path):
    import tomllib

    from zundamon_jarvis.config import update_config

    p = tmp_path / "c.toml"
    original = (
        "# my zundamon-jarvis\n"
        'mic_device = "zundamon_jarvis_aec_source"  # AEC on\n'
        "speed = 1.0\n"
        "future_knob = [1, 2]\n"
        "\n"
        "[extra]\n"
        "speed = 5\n"
    )
    p.write_text(original, encoding="utf-8")
    update_config({"speed": 1.4, "speaker_id": 8}, p)
    text = p.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["speed"] == 1.4 and data["speaker_id"] == 8
    assert data["mic_device"] == "zundamon_jarvis_aec_source"
    assert data["future_knob"] == [1, 2]
    assert data["extra"] == {"speed": 5}  # a table's key of the same name is not touched
    assert text.startswith("# my zundamon-jarvis\n")
    assert "# AEC on" in text
    assert text.count("speed =") == 2  # replaced in place, not appended


def test_update_writes_readable_values(tmp_path):
    from zundamon_jarvis.config import update_config

    p = tmp_path / "c.toml"
    update_config({"model": 'he said "hi"', "speed": 1.2}, p)
    assert 'speed = 1.2\n' in p.read_text(encoding="utf-8")
    assert load_config(p).model == 'he said "hi"'


def test_update_with_none_removes_the_key(tmp_path):
    from zundamon_jarvis.config import update_config

    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\nmic_device = \"x\"\n", encoding="utf-8")
    update_config({"speaker_id": None}, p)
    assert load_config(p).speaker_id is None
    assert load_config(p).mic_device == "x"


def test_update_refuses_a_broken_file_and_leaves_it(tmp_path):
    import pytest

    from zundamon_jarvis.config import update_config

    p = tmp_path / "c.toml"
    p.write_text("this is = = not toml\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_config({"speed": 1.3}, p)
    assert p.read_text(encoding="utf-8") == "this is = = not toml\n"


def test_mic_device_accepts_a_device_index(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("mic_device = 3\n", encoding="utf-8")
    assert load_config(p).mic_device == 3


def test_mic_device_blank_or_invalid_falls_back_to_default(tmp_path):
    p = tmp_path / "config.toml"
    for bad in ('""', '"  "', "true", "-1", "1.5", "[1]"):
        p.write_text(f"mic_device = {bad}\n", encoding="utf-8")
        assert load_config(p).mic_device is None, bad


# ---- migration from the pre-rename path ----

def test_migrate_copies_the_legacy_config_when_the_new_one_is_missing(tmp_path):
    from zundamon_jarvis.config import migrate_legacy_config

    legacy = tmp_path / "jarvis" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("speed = 1.4\n", encoding="utf-8")
    new = tmp_path / "zundamon-jarvis" / "config.toml"
    assert migrate_legacy_config(new, legacy) is True
    assert new.read_text(encoding="utf-8") == "speed = 1.4\n" and legacy.exists()


def test_migrate_never_overwrites_an_existing_config(tmp_path):
    from zundamon_jarvis.config import migrate_legacy_config

    legacy = tmp_path / "old.toml"
    legacy.write_text("speed = 1.4\n", encoding="utf-8")
    new = tmp_path / "new.toml"
    new.write_text("speed = 1.1\n", encoding="utf-8")
    assert migrate_legacy_config(new, legacy) is False
    assert new.read_text(encoding="utf-8") == "speed = 1.1\n"


def test_migrate_does_nothing_without_a_legacy_file(tmp_path):
    from zundamon_jarvis.config import migrate_legacy_config

    new = tmp_path / "zundamon-jarvis" / "config.toml"
    assert migrate_legacy_config(new, tmp_path / "missing.toml") is False
    assert not new.exists()


def test_load_config_falls_back_to_the_legacy_file_at_the_default_path(tmp_path, monkeypatch):
    from zundamon_jarvis import config

    legacy = tmp_path / "jarvis" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("speed = 1.4\n", encoding="utf-8")
    new = tmp_path / "zundamon-jarvis" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", new)
    monkeypatch.setattr(config, "LEGACY_CONFIG_PATH", legacy)
    assert config.load_config(new).speed == 1.4
    assert new.exists()


def test_workdir_defaults_to_home(tmp_path):
    assert load_config(tmp_path / "missing.toml").workdir == Path.home()
    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\n", encoding="utf-8")
    assert load_config(p).workdir == Path.home()


def test_workdir_from_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "notes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    p = tmp_path / "c.toml"
    p.write_text('workdir = "~/notes"\n', encoding="utf-8")
    assert load_config(p).workdir == home / "notes"


def test_knowledge_dir_optional(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "kb").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    p = tmp_path / "c.toml"
    p.write_text("speaker_id = 3\n", encoding="utf-8")
    assert load_config(p).knowledge_dir is None
    p.write_text('knowledge_dir = "~/kb"\n', encoding="utf-8")
    assert load_config(p).knowledge_dir == home / "kb"


def test_missing_workdir_falls_back_to_home_with_warning(tmp_path, caplog):
    p = tmp_path / "c.toml"
    p.write_text(f'workdir = {json.dumps(str(tmp_path / "nope"))}\n', encoding="utf-8")
    assert load_config(p).workdir == Path.home()
    assert "workdir" in caplog.text


def test_duplex_defaults_to_auto_and_is_validated(tmp_path, caplog):
    assert load_config(tmp_path / "missing.toml").duplex == "auto"
    p = tmp_path / "c.toml"
    for value in ("full", "half", "auto"):
        p.write_text(f'duplex = "{value}"\n', encoding="utf-8")
        assert load_config(p).duplex == value
    p.write_text('duplex = "simplex"\n', encoding="utf-8")
    assert load_config(p).duplex == "auto"
    assert "duplex" in caplog.text
