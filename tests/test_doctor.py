import json
import subprocess
import sys
from pathlib import Path

import pytest

from zundamon_jarvis import doctor


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "Users" / "taro"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))  # Path.home() on Windows
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
        + f"WARNING config: knowledge_dir '{home}/notes' for taro (taro@example.com)\n", encoding="utf-8")
    (logs / "2026-10-24.jsonl").write_text(json.dumps(
        {"ts": "2026-10-24T10:00:00+09:00", "heard": "銀行の暗証番号は", "replied": "言わないで",
         "state": "listening"}, ensure_ascii=False) + "\n", encoding="utf-8")
    cfg = home / "Library" / "Application Support" / "zundamon-jarvis" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'knowledge_dir = {json.dumps(f"{home}/notes")}\nmodel = "opus"\n', encoding="utf-8")
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
        'INFO:     127.0.0.1:50124 - "POST /synthesis?speaker=3 HTTP/1.1" 200 OK\n', encoding="utf-8")
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
    ]) + "\n", encoding="utf-8")
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
    cfg.write_text('knowledge_dir = "~/Documents/秘密プロジェクト"\nspeed = 1.4\n', encoding="utf-8")
    logs = home / "Library" / "Logs" / "zundamon-jarvis"
    logs.mkdir(parents=True)
    (logs / "daemon.log").write_text(f"WARNING config: knowledge_dir {notes} is not an existing directory\n", encoding="utf-8")
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
    py.write_text("", encoding="utf-8")
    exe.write_text(f"#!{py}\nimport sys\n", encoding="utf-8")
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
    monkeypatch.setattr(doctor.getpass, "getuser", lambda: "taro")
    if sys.platform == "win32":  # no pwd: the full name comes from the Windows account
        monkeypatch.setattr(doctor, "_windows_full_name", lambda: "山田 太郎")
    else:
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: type("P", (), {"pw_gecos": "山田 太郎,,,"})())
    assert set(doctor.user_names()) == {"taro", "山田 太郎", "山田", "太郎"}


# ---- Windows: no pactl / systemctl / journalctl / launchctl ----

def test_doctor_on_windows_reads_files_registry_and_gpus(home, tmp_path):
    seen = []

    def run(argv, **kw_):
        seen.append(argv[0])
        assert argv[0] in ("powershell", "C:/bin/claude.exe"), argv
        out = "NVIDIA GeForce RTX 4070\r\nIntel(R) UHD Graphics 770\r\n" if argv[0] == "powershell" \
            else "2.1.0 (Claude Code)\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    logs = tmp_path / "Local" / "zundamon-jarvis" / "logs"
    logs.mkdir(parents=True)
    (logs / "daemon.log").write_text("INFO zundamon_jarvis.daemon: running (listening)\n", encoding="utf-8")
    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(
        home, system="windows", run=run, log_dir=logs,
        which=lambda n: "C:/bin/claude.exe" if n == "claude" else None,
        cuda_devices=lambda: 0,
        autostart=lambda: r'"C:\Users\taro\.local\bin\zundamon-jarvis.exe" supervise')) == 0
    out = "\n".join(lines)
    assert "Windows" in out and "GPU: NVIDIA GeForce RTX 4070, Intel(R) UHD Graphics 770" in out
    assert "CUDA デバイス: 0（音声認識は CPU（int8、遅め））" in out
    assert "running (listening)" in out  # the daemon log file, not the journal
    assert r"Run\ZundamonJarvis): " in out and "zundamon-jarvis.exe" in out and "supervise" in out
    assert "サービス:" not in out  # service_status (systemctl) is not asked
    assert set(seen) == {"powershell", "C:/bin/claude.exe"}


def test_doctor_on_windows_survives_missing_probes(home, tmp_path):
    def run(argv, **kw_):
        raise FileNotFoundError(argv[0])

    def no_ct2():
        raise ImportError("DLL load failed while importing _ext")

    def no_winreg():
        raise ModuleNotFoundError("No module named 'winreg'")

    lines: list[str] = []
    assert doctor.run_doctor(lines.append, **kw(
        home, system="windows", run=run, log_dir=tmp_path / "nope",
        cuda_devices=no_ct2, autostart=no_winreg)) == 0
    out = "\n".join(lines)
    assert "CUDA デバイス: ImportError: DLL load failed" in out
    assert "GPU: FileNotFoundError" in out
    assert "読めません（ModuleNotFoundError" in out
    lines = []
    doctor.run_doctor(lines.append, **kw(home, system="windows", run=run, log_dir=tmp_path,
                                         cuda_devices=lambda: 1, autostart=lambda: None))
    out = "\n".join(lines)
    assert "登録されていません" in out and "CUDA デバイス: 1（音声認識は CUDA）" in out


def test_redact_windows_home_in_every_spelling():
    home = r"C:\Users\taro"
    text = "\n".join([r"C:\Users\taro\AppData\Local\zundamon-jarvis\logs",
                      "C:/Users/taro/notes",
                      json.dumps({"path": r"C:\Users\taro\x"})])
    out = doctor.redact(text, home=home)
    assert "Users" not in out
    assert out.splitlines() == [r"~\AppData\Local\zundamon-jarvis\logs", "~/notes", r'{"path": "~\\x"}']


def test_windows_mic_silence_verdict_points_at_the_privacy_settings():
    silent = {"frames_last_5s": 156, "max_abs_last_5s": 0.0}
    assert doctor.mic_verdict(silent, "windows") == doctor.WINDOWS_MIC_SILENT_VERDICT
    assert "プライバシー" in doctor.WINDOWS_MIC_SILENT_VERDICT
    assert doctor.mic_verdict(silent) == doctor.mic_verdict(silent, "mac") == doctor.MIC_SILENT_VERDICT


def test_windows_user_names_use_the_display_name(monkeypatch):
    monkeypatch.setattr(doctor.getpass, "getuser", lambda: "taro")
    monkeypatch.setattr(doctor, "_windows_full_name", lambda: "Taro Yamada")
    assert set(doctor.user_names("windows")) == {"taro", "Taro Yamada", "Taro", "Yamada"}

    def boom():
        raise AttributeError("module 'ctypes' has no attribute 'windll'")

    monkeypatch.setattr(doctor, "_windows_full_name", boom)
    assert doctor.user_names("windows") == ["taro"]
