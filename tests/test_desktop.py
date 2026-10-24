import json
import subprocess

import pytest

from zundamon_jarvis import desktop

CMD = ["/x/zundamon-jarvisctl", "watch"]


class Fake:
    def __init__(self, clients=(), replies=None):
        self.clients = list(clients)
        self.replies = replies or {}
        self.runs = []
        self.spawned = []

    def run(self, argv, **kw):
        self.runs.append(argv)
        if argv[:2] == ["hyprctl", "clients"]:
            out = json.dumps(self.clients)
        elif argv[0] == "osascript":
            out = next((v for k, v in self.replies.items() if k in " ".join(argv)), "0")
        else:
            out = self.replies.get("dispatch", "ok")
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def spawn(self, argv, **kw):
        self.spawned.append((argv, kw))


@pytest.fixture
def hypr_env(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_hyprland_path_unchanged_opens(hypr_env):
    f = Fake([{"class": "foot", "address": "0x1"}])
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    (argv, kw), = f.spawned
    assert argv == ["/usr/bin/omarchy-launch-tui", "--app-id=zundamon-jarvis-watch", *CMD]
    assert f.runs == [["hyprctl", "clients", "-j"]]


def test_hyprland_path_unchanged_closes(hypr_env):
    f = Fake([{"class": "zundamon-jarvis-settings", "address": "0xabc"}])
    desktop.toggle_window("settings", ["x", "settings"], system="linux", run=f.run, spawn=f.spawn)
    assert f.runs[1] == ["hyprctl", "dispatch", 'hl.dsp.window.close({ window = "address:0xabc" })']
    assert f.spawned == []


def test_hyprland_close_falls_back_to_classic_dispatcher(hypr_env):
    f = Fake([{"class": "zundamon-jarvis-watch", "address": "0xabc"}], {"dispatch": "error"})
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    assert f.runs[-1] == ["hyprctl", "dispatch", "closewindow", "address:0xabc"]


def test_hyprland_without_omarchy_launcher_uses_xdg_terminal_exec(monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "sig")
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/bin/hyprctl" if n == "hyprctl" else None)
    f = Fake([])
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    assert f.spawned[0][0] == ["xdg-terminal-exec", "--app-id=zundamon-jarvis-watch", "-e", *CMD]


def test_mac_opens_terminal_with_title(monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda n: None)
    f = Fake(replies={'"iTerm" is running': "false", '"Terminal" is running': "true", "count": "0"})
    desktop.toggle_window("watch", ["/Users/a b/zundamon-jarvisctl", "watch"], system="mac", run=f.run, spawn=f.spawn)
    script = f.runs[-1][-1]
    assert f.runs[-1][:2] == ["osascript", "-e"]
    assert 'tell application "Terminal"' in script and "do script" in script
    assert "ZundamonJarvis — watch" in script
    assert "'/Users/a b/zundamon-jarvisctl' watch" in script
    assert f.spawned == []


def test_mac_escapes_for_applescript():
    assert desktop._applescript_quote('a"b\\c') == '"a\\"b\\\\c"'


def test_mac_uses_iterm_when_running():
    f = Fake(replies={'"iTerm" is running': "true", "count": "0"})
    desktop.toggle_window("settings", ["/x/ctl", "settings"], system="mac", run=f.run)
    script = f.runs[-1][-1]
    assert 'tell application "iTerm"' in script and "ZundamonJarvis — settings" in script


def test_mac_closes_when_window_exists():
    f = Fake(replies={'"iTerm" is running': "false", '"Terminal" is running': "true", "count": "1"})
    desktop.toggle_window("watch", CMD, system="mac", run=f.run)
    script = f.runs[-1][-1]
    assert "close" in script and "do script" not in script and "ZundamonJarvis — watch" in script
    assert script.endswith("saving no")
    assert f.runs.index(["pkill", "-f", r"/x/zundamon\-jarvisctl\ watch"]) < f.runs.index(f.runs[-1])


def test_mac_open_does_not_activate_before_do_script():
    f = Fake(replies={'"iTerm" is running': "false", '"Terminal" is running': "false"})
    desktop.toggle_window("watch", CMD, system="mac", run=f.run)
    script = f.runs[-1][-1]
    assert script.index("do script") < script.index("activate")


def test_osascript_failure_is_logged(caplog):
    def run(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
    desktop.toggle_window("watch", CMD, system="mac", run=run)
    assert "osascript failed" in caplog.text


def test_generic_linux_uses_terminal_env(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setenv("TERMINAL", "kitty")
    monkeypatch.setattr(desktop.shutil, "which", lambda n: f"/usr/bin/{n}")
    f = Fake()
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    assert f.spawned[0][0] == ["/usr/bin/kitty", "-e", *CMD]
    assert f.runs == []


def test_generic_linux_xdg_terminal_exec_takes_command_directly(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: f"/usr/bin/{n}")
    f = Fake()
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    assert f.spawned[0][0] == ["/usr/bin/xdg-terminal-exec", *CMD]


def test_generic_linux_falls_back_to_x_terminal_emulator(monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: "/usr/bin/x-terminal-emulator" if n == "x-terminal-emulator" else None)
    f = Fake()
    desktop.toggle_window("watch", CMD, system="linux", run=f.run, spawn=f.spawn)
    assert f.spawned[0][0] == ["/usr/bin/x-terminal-emulator", "-e", *CMD]


def test_no_terminal_found_prints_command(monkeypatch, capsys):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda n: None)
    f = Fake()
    with pytest.raises(SystemExit) as e:
        desktop.toggle_window("watch", ["/x/ctl", "watch"], system="linux", run=f.run, spawn=f.spawn)
    assert e.value.code == 1
    assert "/x/ctl watch" in capsys.readouterr().err
    assert f.spawned == []
