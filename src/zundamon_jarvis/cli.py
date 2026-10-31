"""`zundamon-jarvis daemon [--start-paused]` / `voices [--no-play]` / `menubar` (macOS) /
`service` / `engine install` / `models download` / `setup` / `doctor` / `uninstall`."""

import sys

USAGE = ("usage: zundamon-jarvis setup [--cli] | zundamon-jarvis doctor | zundamon-jarvis uninstall"
         " | zundamon-jarvis daemon [--start-paused] | zundamon-jarvis voices [--no-play]"
         " | zundamon-jarvis menubar | zundamon-jarvis service install|uninstall|status"
         " | zundamon-jarvis engine install [--cpu] | zundamon-jarvis models download")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd, rest = (args[0], args[1:]) if args else ("", [])
    if cmd in ("--version", "version") and not rest:
        from importlib.metadata import version

        print(version("zundamon-jarvis"))
        return 0
    if cmd == "daemon":
        from zundamon_jarvis.daemon import main as daemon_main

        return daemon_main(rest)
    if cmd == "voices":
        from zundamon_jarvis.voices import main as voices_main

        return voices_main(rest)
    if cmd == "menubar" and not rest:
        from zundamon_jarvis.menubar import run_menubar

        return run_menubar()
    if cmd == "service" and len(rest) == 1 and rest[0] in ("install", "uninstall", "status"):
        from zundamon_jarvis import platform, service

        sys_ = platform.system()
        if rest[0] == "install":
            service.install_services(sys_)
        elif rest[0] == "uninstall":
            service.uninstall_services(sys_)
        else:
            print(service.service_status(sys_))
        return 0
    if cmd == "engine" and rest[:1] == ["install"] and rest[1:] in ([], ["--cpu"]):
        from zundamon_jarvis import engine, platform

        flavor = "linux-cpu" if rest[1:] == ["--cpu"] else None
        try:
            engine.install_engine(platform.system(), flavor=flavor)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        return 0
    if cmd == "models" and rest == ["download"]:
        from zundamon_jarvis import stt

        print("文字起こしのモデルを取得しています（初回は数 GB あり、時間がかかります）…", flush=True)
        try:
            where = stt.download_model()
        except Exception as e:  # network, disk: say what happened, the installer stops here
            print(f"モデルを取得できませんでした: {e}", file=sys.stderr)
            return 1
        print(f"モデルの準備ができました: {where}")
        return 0
    if cmd == "setup":  # the web page; --cli: the terminal questions; --demo: the page with fakes
        from zundamon_jarvis import setup_gui

        return setup_gui.main(rest)
    if cmd == "doctor":
        from zundamon_jarvis import doctor

        return doctor.main(rest)
    if cmd == "uninstall":
        from zundamon_jarvis import setup_wizard

        return setup_wizard.uninstall_main(rest)
    print(USAGE, file=sys.stderr)
    return 2
