"""Always-on services: launchd agents on macOS, systemd user units on Linux.

On Linux the on-screen character (``zundamon-jarvisctl character``) also goes into the app
launcher: a desktop entry plus its icon, rewritten on every install (the path to
``zundamon-jarvisctl`` may have changed).
"""

import os
import plistlib
from importlib import resources
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from zundamon_jarvis import platform

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


def _find_exe() -> str:
    sibling = Path(sys.executable).parent / "zundamon-jarvis"
    if sibling.exists():
        return str(sibling)
    found = shutil.which("zundamon-jarvis")
    if not found:
        raise RuntimeError("zundamon-jarvis executable not found; install it first (uv tool install .)")
    return str(Path(found).absolute())


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


def install_services(
    system: str,
    run: Callable = subprocess.run,
    home: Path | None = None,
    uid: int | None = None,
    exe: str | None = None,
    claude: str | None = None,
    unit_src: Path | None = None,
    ctl: str | None = None,
) -> None:
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
) -> None:
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


def service_status(system: str, run: Callable = subprocess.run, uid: int | None = None) -> str:
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
