import os
from pathlib import Path

import pytest

from zundamon_jarvis.platform import config_dir, data_dir, log_dir, runtime_dir, socket_path, system


def test_linux_paths_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert config_dir("linux") == tmp_path / ".config/zundamon-jarvis"
    assert log_dir("linux") == tmp_path / ".local/state/zundamon-jarvis"
    assert data_dir("linux") == tmp_path / ".local/share/zundamon-jarvis"
    assert socket_path("linux") == Path("/run/user/1000/zundamon-jarvis.sock")


def test_linux_honours_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_dir("linux") == tmp_path / "zundamon-jarvis"


def test_linux_runtime_dir_falls_back_to_run_user(monkeypatch):
    import os
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert runtime_dir("linux") == Path(f"/run/user/{os.getuid()}")


def test_mac_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "t"))
    sup = tmp_path / "Library/Application Support/zundamon-jarvis"
    assert config_dir("mac") == sup
    assert data_dir("mac") == sup
    assert log_dir("mac") == tmp_path / "Library/Logs/zundamon-jarvis"
    sock = socket_path("mac")
    assert sock.parent.stat().st_mode & 0o777 == 0o700
    assert sock.name == "zundamon-jarvis.sock"
    assert sock.parent == tmp_path / "t" / f"zundamon-jarvis-{os.getuid()}"


def test_system_from_sys_platform(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert system() == "mac"
    monkeypatch.setattr("sys.platform", "linux")
    assert system() == "linux"


def test_mac_without_tmpdir_uses_tmp_with_uid(monkeypatch, tmp_path):
    monkeypatch.delenv("TMPDIR", raising=False)
    d = Path("/tmp") / f"zundamon-jarvis-{os.getuid()}"
    if d.exists() and (d.is_symlink() or d.lstat().st_uid != os.getuid()):
        pytest.skip("unusable pre-existing dir")
    assert runtime_dir("mac") == d
    assert d.stat().st_mode & 0o777 == 0o700


def test_mac_runtime_dir_tightens_wrong_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    d = tmp_path / f"zundamon-jarvis-{os.getuid()}"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    assert runtime_dir("mac") == d
    assert d.stat().st_mode & 0o777 == 0o700


def test_mac_runtime_dir_rejects_symlink(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / f"zundamon-jarvis-{os.getuid()}").symlink_to(target)
    with pytest.raises(RuntimeError, match="not a real directory"):
        runtime_dir("mac")


def test_mac_runtime_dir_rejects_foreign_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    uid = os.getuid()
    (tmp_path / f"zundamon-jarvis-{uid}").mkdir(mode=0o700)
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    # the dir name now uses uid+1, so point at the existing one via a matching name
    (tmp_path / f"zundamon-jarvis-{uid + 1}").mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match="owned by uid"):
        runtime_dir("mac")


def test_windows_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert config_dir("windows") == tmp_path / "Roaming" / "zundamon-jarvis"
    assert data_dir("windows") == tmp_path / "Local" / "zundamon-jarvis"
    assert log_dir("windows") == tmp_path / "Local" / "zundamon-jarvis" / "logs"
    assert socket_path("windows") == tmp_path / "Local" / "zundamon-jarvis" / "run" / "control.json"
    assert (tmp_path / "Local" / "zundamon-jarvis" / "run").is_dir()


def test_system_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert system() == "windows"
