import json
import subprocess
from pathlib import Path

import pytest

from zundamon_jarvis import doctor


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "Users" / "taro"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def down(cmd, timeout=3.0):
    raise ConnectionRefusedError("no daemon")


def no_voicevox():
    raise OSError("connection refused")


def fake_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 0, stdout=f"{argv[0]}: fine\n", stderr="")


def kw(home, **over):
    base = dict(system="mac", request=down, voicevox_version=no_voicevox,
                mic_names=lambda: ["MacBook Proのマイク", "Taro's AirPods"],
                run=fake_run, which=lambda name: None, names=["taro", "Taro Yamada"],
                hostnames=["Taros-MacBook-Pro.local"])
    base.update(over)
    return base


def test_redact_rules(home):
    text = "\n".join([
        f"{home}/Library/Logs/zundamon-jarvis/daemon.log",
        "INFO user taro logged in as taro.yamada+jj@example.co.jp",
        json.dumps({"ts": "2026-10-24T10:00:00+09:00", "heard": "秘密の話",
                    "replied": "秘密の返事", "state": "listening"}, ensure_ascii=False),
        "repr {'heard': 'ひみつ', 'replied': \"また \\\"ね\\\"\"}",
        "host Taros-MacBook-Pro.local",
    ])
    out = doctor.redact(text, home=home, names=["taro"], hostnames=["Taros-MacBook-Pro.local"])
    assert str(home) not in out and "~/Library/Logs" in out
    assert "taro" not in out.lower() and "example.co.jp" not in out
    assert "秘密" not in out and "ひみつ" not in out and "また" not in out
    assert '"heard": "…"' in out and '"replied": "…"' in out and "'heard': '…'" in out
    assert '"state": "listening"' in out


def test_doctor_redacts_home_user_email_and_conversation(home):
    logs = home / "Library" / "Logs" / "zundamon-jarvis"
    logs.mkdir(parents=True)
    (logs / "daemon.log").write_text(
        "".join(f"INFO line {i}\n" for i in range(300))
        + f"WARNING config: knowledge_dir '{home}/notes' for taro (taro@example.com)\n")
    (logs / "2026-10-24.jsonl").write_text(json.dumps(
        {"ts": "2026-10-24T10:00:00+09:00", "heard": "銀行の暗証番号は", "replied": "言わないで",
         "state": "listening"}, ensure_ascii=False) + "\n")
    cfg = home / "Library" / "Application Support" / "zundamon-jarvis" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'knowledge_dir = "{home}/notes"\nmodel = "opus"\n')
    lines: list[str] = []
    assert doctor.run_doctor(lines.append, config_path=cfg, **kw(home)) == 0
    out = "\n".join(lines)
    assert str(home) not in out
    assert "taro" not in out.lower() and "yamada" not in out.lower()
    assert "example.com" not in out and "Taros-MacBook-Pro" not in out
    assert "銀行" not in out and "言わないで" not in out and '"heard": "…"' in out
    log_lines = out.splitlines()  # only the last 200 lines of the daemon log
    assert "INFO line 101" in log_lines and "INFO line 100" not in log_lines
    assert 'model = "opus"' in out


def test_doctor_runs_without_daemon(home):
    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(home)) == 0
    out = "\n".join(lines)
    assert "Python" in out and "zundamon-jarvis" in out
    assert "動いていません" in out          # daemon status
    assert "つながりません" in out          # VOICEVOX
    assert "MacBook Proのマイク" in out
    assert "<user>'s AirPods" in out


def test_doctor_reports_running_daemon_and_login(home):
    def up(cmd, timeout=3.0):
        if cmd == "debug":
            return {"state": "listening", "text": "Listening", "login": False,
                    "debug": {"frames_last_5s": 156}}
        return {"state": "listening", "text": "Listening", "login": False}

    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(home, request=up,
                                                voicevox_version=lambda: "0.26.0")) == 0
    out = "\n".join(lines)
    assert "0.26.0" in out and "frames_last_5s" in out
    assert "ログインしていません" in out


def test_doctor_linux_reads_journal(home):
    seen = []

    def run(argv, **kw_):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="journal line\n", stderr="")

    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(home, system="linux", run=run,
                                                which=lambda n: f"/usr/bin/{n}")) == 0
    assert ["journalctl", "--user", "-u", "zundamon-jarvis", "-n", "200", "--no-pager"] in seen
    assert "journal line" in "\n".join(lines)


SECRET_URL = "%E7%A7%98%E5%AF%86%E3%81%AE%E8%A9%B1"  # 秘密の話, as VOICEVOX's access log has it


def test_doctor_hides_speech_in_voicevox_and_old_daemon_logs(home):
    logs = home / "Library" / "Logs" / "zundamon-jarvis"
    logs.mkdir(parents=True)
    (logs / "voicevox.log").write_text(
        f'INFO:     127.0.0.1:50123 - "POST /audio_query?text={SECRET_URL}&speaker=3 HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:50124 - "POST /synthesis?speaker=3 HTTP/1.1" 200 OK\n')
    (logs / "daemon.log").write_text("\n".join([
        "ERROR zundamon_jarvis.orchestrator: speak failed: '秘密の返事です。'",
        "Traceback (most recent call last):",
        '  File "/x/orchestrator.py", line 726, in _speak_loop',
        "httpx.HTTPStatusError: Server error '500 Internal Server Error' for url "
        f"'http://127.0.0.1:50021/audio_query?text={SECRET_URL}&speaker=3'",
        "ERROR zundamon_jarvis.orchestrator: failed to handle TextDelta(text='内緒の話')",
        "INFO zundamon_jarvis.orchestrator: no answer to '「ジム」をカレンダーに入れるけど、いい？'; denying",
        "WARNING zundamon_jarvis.brain: Claude turn failed: ResultMessage(subtype='error', result='返事の一部')",
        "INFO zundamon_jarvis.orchestrator: utterance dropped (paused): '独り言'",
        "WARNING zundamon_jarvis.orchestrator: speak failed (12 chars): HTTPStatusError",
    ]) + "\n")
    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(home)) == 0
    out = "\n".join(lines)
    for secret in (SECRET_URL, "%E7", "秘密", "内緒", "ジム", "返事の一部", "独り言"):
        assert secret not in out
    assert "/audio_query?text=…&speaker=3" in out and "/synthesis?speaker=3" in out
    assert "failed to handle TextDelta(…)" in out and "utterance dropped (paused): …" in out
    assert "speak failed (12 chars): HTTPStatusError" in out  # the new format is kept as is


def test_doctor_hides_the_knowledge_dir(home):
    notes = home / "Documents" / "秘密プロジェクト"
    notes.mkdir(parents=True)
    cfg = home / "Library" / "Application Support" / "zundamon-jarvis" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('knowledge_dir = "~/Documents/秘密プロジェクト"\nspeed = 1.4\n')
    logs = home / "Library" / "Logs" / "zundamon-jarvis"
    logs.mkdir(parents=True)
    (logs / "daemon.log").write_text(f"WARNING config: knowledge_dir {notes} is not an existing directory\n")
    lines: list[str] = []
    assert doctor.run_doctor(lines.append, config_path=cfg, **kw(home)) == 0
    out = "\n".join(lines)
    assert "秘密プロジェクト" not in out
    assert "knowledge_dir = <設定あり>" in out and "knowledge_dir <knowledge_dir> is not" in out
    assert "speed = 1.4" in out


def test_doctor_names_the_daemon_python_and_flags_digital_silence(home):
    def up(cmd, timeout=3.0):
        base = {"state": "listening", "text": "Listening"}
        if cmd == "debug":
            return {**base, "debug": {"paused": False, "frames_last_5s": 156, "max_abs_last_5s": 0.0,
                                      "python": f"{home}/.local/share/uv/python/bin/python3.12"}}
        return base

    lines: list[str] = []
    doctor.run_doctor(lines.append, **kw(home, request=up))
    out = "\n".join(lines)
    assert "デーモンの Python: ~/.local/share/uv/python/bin/python3.12" in out
    assert doctor.MIC_SILENT_VERDICT in out
    assert doctor.mic_verdict({"frames_last_5s": 156, "max_abs_last_5s": 0.02}) is None
    assert doctor.mic_verdict({"paused": True, "frames_last_5s": 0, "max_abs_last_5s": None}) is None


def test_doctor_reads_the_daemon_python_from_the_launch_agent(home):
    import plistlib

    exe = home / ".local" / "bin" / "zundamon-jarvis"
    exe.parent.mkdir(parents=True)
    py = home / ".local" / "share" / "uv" / "tools" / "zundamon-jarvis" / "bin" / "python3.12"
    py.parent.mkdir(parents=True)
    py.write_text("")
    exe.write_text(f"#!{py}\nimport sys\n")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "dev.zundamon-jarvis.daemon.plist").write_bytes(
        plistlib.dumps({"ProgramArguments": [str(exe), "daemon"]}))
    lines: list[str] = []
    doctor.run_doctor(lines.append, **kw(home))
    assert "デーモンの Python（起動設定から）: ~/.local/share/uv/tools/zundamon-jarvis/bin/python3.12" in "\n".join(lines)


def test_short_names_match_only_whole_words_and_cjk_parts_count(monkeypatch):
    out = doctor.redact("alice logged in; alicia's mic; /x/alice/y", home=Path("/nowhere"), names=["alice"])
    assert out == "<user> logged in; alicia's mic; /x/<user>/y"
    import pwd

    monkeypatch.setattr(doctor.getpass, "getuser", lambda: "taro")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_gecos": "山田 太郎,,,"})())
    assert set(doctor.user_names()) == {"taro", "山田 太郎", "山田", "太郎"}
