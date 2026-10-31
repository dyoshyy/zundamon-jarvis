"""Always-on services: launchd agents on macOS, systemd user units on Linux, and on Windows a
login entry for the supervisor.

On Linux the on-screen character (``zundamon-jarvisctl character``) also goes into the app
launcher: a desktop entry plus its icon, rewritten on every install (the path to
``zundamon-jarvisctl`` may have changed).

Windows has no per-user service manager that needs no admin rights, so ``zundamon_jarvis.supervisor``
plays systemd's part, and the ``Run`` key (``HKCU\\...\\CurrentVersion\\Run``, value
``ZundamonJarvis``) starts it at login through the windowless ``zundamon-jarvisw.exe`` (uv makes
it from ``[project.gui-scripts]``; the console ``zundamon-jarvis.exe`` would open a window).
Install also starts it now, detached; an already running one is stopped first so a re-run (the
setup wizard installs on every run) picks up the new config, as ``bootout`` + ``bootstrap`` does
on macOS. The registry and the process functions are parameters, for the tests.
"""

import os
import plistlib
from importlib import resources
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from zundamon_jarvis import platform, supervisor

LABELS = {
    "daemon": "dev.zundamon-jarvis.daemon",
    "voicevox": "dev.zundamon-jarvis.voicevox",
    "menubar": "dev.zundamon-jarvis.menubar",
}
UNITS = ("voicevox.service", "zundamon-jarvis.service")
DESKTOP_ENTRY = Path(".local/share/applications/zundamon-jarvis.desktop")  # under home
ICON = Path(".local/share/icons/hicolor/256x256/apps/zundamon-jarvis.png")


def _base_path(home: Path) -> list[str]:
    return [str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]


def launch_agent_plist(
    label: str,
    argv: list[str],
    log: Path,
    keep_alive: bool | dict = True,
    extra_path: list[str] | None = None,
    home: Path | None = None,
) -> bytes:
    path: list[str] = []
    for d in _base_path(home or Path.home()) + list(extra_path or []):
        if d not in path:
            path.append(d)
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": list(argv),
            "RunAtLoad": True,
            "KeepAlive": keep_alive,
            "ProcessType": "Interactive",
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
            # No ANTHROPIC_API_KEY on purpose: the claude CLI must use the subscription login.
            "EnvironmentVariables": {"PATH": ":".join(path), "PYTHONUNBUFFERED": "1"},
        }
    )


def _script(name: str, system: str | None = None) -> str | None:
    """This install's entry-point script ``name`` (``name.exe`` on Windows): beside the running
    Python first (the tool's own venv), else on PATH."""
    suffix = ".exe" if (system or platform.system()) == "windows" else ""
    sibling = Path(sys.executable).parent / f"{name}{suffix}"
    if sibling.exists():
        return str(sibling)
    found = shutil.which(name)
    return str(Path(found).absolute()) if found else None


def find_exe(system: str | None = None) -> str:
    found = _script("zundamon-jarvis", system)
    if not found:
        raise RuntimeError("zundamon-jarvis executable not found; install it first (uv tool install .)")
    return found


def _find_exe() -> str:
    return find_exe()


def _find_ctl() -> str:
    sibling = Path(sys.executable).parent / "zundamon-jarvisctl"
    if sibling.exists():
        return str(sibling)
    found = shutil.which("zundamon-jarvisctl")
    return str(Path(found).absolute()) if found else "zundamon-jarvisctl"


def desktop_entry(ctl: str) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=ZundamonJarvis",
        "Comment=The voice partner on your screen (click: conversation, right-click: speech bubble)",
        f"Exec={ctl} character",
        "Icon=zundamon-jarvis",
        "Terminal=false",
        "Categories=Utility;",
        "Keywords=jarvis;zundamon;voice;assistant;",
        "",
    ])


def _install_launcher(home: Path, ctl: str | None) -> None:
    entry, icon = home / DESKTOP_ENTRY, home / ICON
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(str(resources.files("zundamon_jarvis") / "assets" / "icon.png")), icon)
    entry.write_text(desktop_entry(ctl or _find_ctl()))


def _agents(exe: str, home: Path) -> dict[str, list[str]]:
    engine_run = str(home / "Library" / "Application Support" / platform.APP / "voicevox_engine" / "run")
    return {
        "voicevox": [engine_run, "--host", "127.0.0.1", "--port", "50021"],
        "daemon": [exe, "daemon"],
        "menubar": [exe, "menubar"],
    }


# "Quit menu bar" must stay quit; a crash restarts it.
KEEP_ALIVE = {"menubar": {"SuccessfulExit": False}}


# ---- Windows ----

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Settings > Apps > Startup writes its on/off switch here, per Run value
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
RUN_VALUE = "ZundamonJarvis"

# subprocess only defines these on Windows
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class Registry(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
    def approved(self, name: str) -> bool | None: ...


class RunKey:
    """The current user's ``Run`` key, through ``winreg`` (Windows only, imported on use)."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                return str(winreg.QueryValueEx(k, name)[0])
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass

    def approved(self, name: str) -> bool | None:
        """False when the user switched it off in Startup apps; None when Windows has no say yet."""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
                data = winreg.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None
        return startup_approved(data)


def startup_approved(data: Any) -> bool | None:
    """StartupApproved's 12-byte value: an even first byte (02, 06) is on, an odd one (03) off."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    return data[0] % 2 == 0


def find_exew(system: str | None = "windows") -> str:
    """The windowless launcher; the console one (a console window at every login) if uv made none."""
    return _script("zundamon-jarvisw", system) or find_exe(system)


def run_entry(exew: str) -> str:
    """The Run value: the path quoted (it has spaces in many profiles), then the command."""
    return f'"{exew}" supervise'


def start_detached(argv: list[str]) -> None:
    """Start ``argv`` with no console and outside our job: the wizard runs under uv's launcher,
    whose job object kills what it holds when the launcher exits."""
    kw: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              cwd=str(Path.home()), close_fds=True)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:  # a job that does not allow breaking away (the child is still silently freed by uv's)
        subprocess.Popen(argv, creationflags=flags, **kw)


def _install_windows(registry: Registry, start: Callable[[list[str]], Any], stop: Callable[[], Any],
                     exew: str | None) -> None:
    exew = exew or find_exew()
    registry.set(RUN_VALUE, run_entry(exew))
    stop()  # a running supervisor would keep the old config (and a second one exits at once)
    start([exew, "supervise"])


def start_services(system: str, start: Callable[[list[str]], Any] = start_detached,
                   exew: str | None = None, running: Callable[[], bool] | None = None) -> bool:
    """Windows: start the supervisor now unless it runs; True if one was started."""
    if (running or (lambda: supervisor.running_pid(system) is not None))():
        return False
    exew = exew or find_exew(system)
    start([exew, "supervise"])
    return True


def stop_services(system: str, stop: Callable[[], str] | None = None) -> str:
    """Windows: stop the supervisor and everything it runs (it stays registered for the next login)."""
    return (stop or (lambda: supervisor.stop(system)))()


def install_services(
    system: str,
    run: Callable = subprocess.run,
    home: Path | None = None,
    uid: int | None = None,
    exe: str | None = None,
    claude: str | None = None,
    unit_src: Path | None = None,
    ctl: str | None = None,
    registry: Registry | None = None,
    start: Callable[[list[str]], Any] = start_detached,
    stop: Callable[[], Any] | None = None,
) -> None:
    """``exe`` is the launcher the service runs: ``zundamon-jarvis`` on macOS, the windowless
    ``zundamon-jarvisw`` on Windows."""
    if system == "windows":
        _install_windows(registry or RunKey(), start, stop or (lambda: supervisor.stop(system)), exe)
        return
    home = Path(home) if home else Path.home()
    if system == "mac":
        _install_mac(run, home, os.getuid() if uid is None else uid, exe, claude)
    else:
        _install_launcher(home, ctl)
        _install_linux(run, home, unit_src)


def _install_mac(run, home: Path, uid: int, exe: str | None, claude: str | None) -> None:
    exe = exe or _find_exe()
    claude = claude if claude is not None else shutil.which("claude")
    extra = [str(Path(claude).parent)] if claude else []
    agents_dir = home / "Library" / "LaunchAgents"
    logs = home / "Library" / "Logs" / platform.APP
    agents_dir.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    for key, argv in _agents(exe, home).items():
        label = LABELS[key]
        plist = agents_dir / f"{label}.plist"
        plist.write_bytes(launch_agent_plist(
                label, argv, logs / f"{key}.log", keep_alive=KEEP_ALIVE.get(key, True), extra_path=extra, home=home
            ))
        run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)  # reinstall: ignore "not loaded"
        run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], check=True)


def uninstall_services(
    system: str,
    run: Callable = subprocess.run,
    home: Path | None = None,
    uid: int | None = None,
    registry: Registry | None = None,
    stop: Callable[[], Any] | None = None,
) -> None:
    if system == "windows":
        (registry or RunKey()).delete(RUN_VALUE)
        (stop or (lambda: supervisor.stop(system)))()
        return
    home = Path(home) if home else Path.home()
    if system == "mac":
        uid = os.getuid() if uid is None else uid
        for label in LABELS.values():
            run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            (home / "Library" / "LaunchAgents" / f"{label}.plist").unlink(missing_ok=True)
    else:
        run(["systemctl", "--user", "disable", "--now", *UNITS], capture_output=True)
        for n in UNITS:
            (home / ".config" / "systemd" / "user" / n).unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        (home / DESKTOP_ENTRY).unlink(missing_ok=True)
        (home / ICON).unlink(missing_ok=True)


def _install_linux(run, home: Path, unit_src: Path | None) -> None:
    dest = home / ".config" / "systemd" / "user"
    if all((dest / n).exists() for n in UNITS):
        print("systemd units already installed; nothing to do")
        return
    src = unit_src or Path(str(resources.files("zundamon_jarvis") / "assets" / "systemd"))
    dest.mkdir(parents=True, exist_ok=True)
    for n in UNITS:
        if not (dest / n).exists():
            shutil.copyfile(src / n, dest / n)
    run(["systemctl", "--user", "daemon-reload"], check=True)
    run(["systemctl", "--user", "enable", "--now", *UNITS], check=True)


def _engine_up(url: str = "http://127.0.0.1:50021/version", timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def _daemon_status() -> dict | None:
    from zundamon_jarvis.control import request

    try:
        resp = request("status", timeout=1.0)
    except (OSError, ValueError):
        return None
    return None if "error" in resp else resp


def _windows_status(registry: Registry, supervisor_pid: Callable[[], int | None],
                    daemon: Callable[[], dict | None], engine_up: Callable[[], bool]) -> str:
    entry = registry.get(RUN_VALUE)
    at_login = entry or "not registered"
    if entry and registry.approved(RUN_VALUE) is False:
        at_login += " (switched off in Settings > Apps > Startup)"
    pid = supervisor_pid()
    status = daemon()
    return "\n".join([
        f"autostart (HKCU\\{RUN_KEY}\\{RUN_VALUE}): {at_login}",
        "supervisor: " + ("not running" if pid is None else f"running (pid {pid})" if pid > 0 else "running"),
        "daemon: " + ((status.get("text") or status.get("state") or "running") if status else "not running"),
        "engine: " + ("running" if engine_up() else "not running"),
    ])


def service_status(
    system: str,
    run: Callable = subprocess.run,
    uid: int | None = None,
    registry: Registry | None = None,
    supervisor_pid: Callable[[], int | None] | None = None,
    daemon: Callable[[], dict | None] = _daemon_status,
    engine_up: Callable[[], bool] = _engine_up,
) -> str:
    if system == "windows":
        return _windows_status(registry or RunKey(), supervisor_pid or (lambda: supervisor.running_pid(system)),
                               daemon, engine_up)
    lines = []
    if system == "mac":
        uid = os.getuid() if uid is None else uid
        for label in LABELS.values():
            r = run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True)
            state = "not loaded"
            if r.returncode == 0:
                state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "loaded")
            lines.append(f"{label}: {state}")
    else:
        for n in UNITS:
            r = run(["systemctl", "--user", "is-active", n], capture_output=True, text=True)
            lines.append(f"{n}: {(r.stdout or '').strip() or 'unknown'}")
    return "\n".join(lines)
