import plistlib
from pathlib import Path

from zundamon_jarvis import service


def test_plist_has_no_api_key_and_keeps_alive(tmp_path):
    data = service.launch_agent_plist("dev.zundamon-jarvis.daemon", ["/x/zundamon-jarvis", "daemon"], tmp_path / "d.log")
    p = plistlib.loads(data)
    assert p["Label"] == "dev.zundamon-jarvis.daemon"
    assert p["ProgramArguments"] == ["/x/zundamon-jarvis", "daemon"]
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True
    assert p["ProcessType"] == "Interactive"
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "d.log")
    env = p["EnvironmentVariables"]
    assert "ANTHROPIC_API_KEY" not in env
    parts = env["PATH"].split(":")
    for need in (str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        assert need in parts


def test_plist_path_includes_claude_dir(tmp_path):
    p = plistlib.loads(service.launch_agent_plist("l", ["a"], tmp_path / "l", extra_path=["/opt/claude/bin"]))
    assert "/opt/claude/bin" in p["EnvironmentVariables"]["PATH"].split(":")


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stdout = ""

        return R()


def test_install_services_mac_writes_three_agents(tmp_path):
    run = Runner()
    service.install_services(
        "mac", run=run, home=tmp_path, uid=501, exe="/u/bin/zundamon-jarvis", claude="/c/bin/claude"
    )
    agents = tmp_path / "Library" / "LaunchAgents"
    names = sorted(p.name for p in agents.glob("*.plist"))
    assert names == [
        "dev.zundamon-jarvis.daemon.plist",
        "dev.zundamon-jarvis.menubar.plist",
        "dev.zundamon-jarvis.voicevox.plist",
    ]
    d = plistlib.loads((agents / "dev.zundamon-jarvis.daemon.plist").read_bytes())
    assert d["ProgramArguments"] == ["/u/bin/zundamon-jarvis", "daemon"]
    assert "/c/bin" in d["EnvironmentVariables"]["PATH"].split(":")
    m = plistlib.loads((agents / "dev.zundamon-jarvis.menubar.plist").read_bytes())
    assert m["ProgramArguments"] == ["/u/bin/zundamon-jarvis", "menubar"]
    v = plistlib.loads((agents / "dev.zundamon-jarvis.voicevox.plist").read_bytes())
    assert v["ProgramArguments"][1:] == ["--host", "127.0.0.1", "--port", "50021"]  # like voicevox.service
    assert v["ProgramArguments"][0].endswith("voicevox_engine/run")
    boots = [c for c in run.calls if c[:2] == ["launchctl", "bootstrap"]]
    assert [c[2] for c in boots] == ["gui/501"] * 3
    assert (tmp_path / "Library" / "Logs" / "zundamon-jarvis").is_dir()


def test_uninstall_services_mac(tmp_path):
    run = Runner()
    service.install_services("mac", run=run, home=tmp_path, uid=501, exe="/e", claude=None)
    run.calls.clear()
    service.uninstall_services("mac", run=run, home=tmp_path, uid=501)
    assert not list((tmp_path / "Library" / "LaunchAgents").glob("*.plist"))
    assert len([c for c in run.calls if c[:2] == ["launchctl", "bootout"]]) == 3


def test_install_services_skips_when_present(tmp_path):
    units = tmp_path / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    for n in ("voicevox.service", "zundamon-jarvis.service"):
        (units / n).write_text("x")
    run = Runner()
    service.install_services("linux", run=run, home=tmp_path)
    assert run.calls == []
    assert (units / "voicevox.service").read_text() == "x"


def test_install_services_linux_installs_and_enables(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in ("voicevox.service", "zundamon-jarvis.service"):
        (src / n).write_text(n)
    run = Runner()
    service.install_services("linux", run=run, home=tmp_path / "h", unit_src=src)
    assert (tmp_path / "h/.config/systemd/user/voicevox.service").read_text() == "voicevox.service"
    assert ["systemctl", "--user", "daemon-reload"] in run.calls
    assert ["systemctl", "--user", "enable", "--now", "voicevox.service", "zundamon-jarvis.service"] in run.calls


def test_menubar_agent_restarts_only_on_crash(tmp_path):
    service.install_services("mac", run=Runner(), home=tmp_path, uid=1, exe="/e", claude=None)
    a = tmp_path / "Library/LaunchAgents"
    m = plistlib.loads((a / "dev.zundamon-jarvis.menubar.plist").read_bytes())
    assert m["KeepAlive"] == {"SuccessfulExit": False}
    d = plistlib.loads((a / "dev.zundamon-jarvis.daemon.plist").read_bytes())
    assert d["KeepAlive"] is True


def test_packaged_unit_templates_exist():
    from importlib import resources

    base = resources.files("zundamon_jarvis") / "assets" / "systemd"
    assert (base / "voicevox.service").is_file() and (base / "zundamon-jarvis.service").is_file()
