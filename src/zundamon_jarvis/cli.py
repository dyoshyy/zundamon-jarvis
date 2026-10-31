"""`zundamon-jarvis daemon [--start-paused]` / `voices [--no-play]` / `menubar` (macOS) /
`tray` / `supervise` (Windows) / `service` / `engine install` / `models download` / `setup` /
`doctor` / `update [--check] [--yes]` / `uninstall`."""

import sys

USAGE = ("usage: zundamon-jarvis setup [--cli] | zundamon-jarvis doctor | zundamon-jarvis uninstall"
         " | zundamon-jarvis update [--check] [--yes]"
         " | zundamon-jarvis daemon [--start-paused] | zundamon-jarvis voices [--no-play]"
         " | zundamon-jarvis menubar | zundamon-jarvis tray | zundamon-jarvis supervise"
         " | zundamon-jarvis service install|uninstall|status|start|stop"
         " | zundamon-jarvis engine install [--cpu] | zundamon-jarvis models download")
TEXT = {  # zundamon_jarvis.i18n: the display language
    "model_fetching": {"ja": "文字起こしのモデルを取得しています（初回は数 GB あり、時間がかかります）…",
                       "en": "Downloading the speech-to-text model (several GB the first time; this takes a while)…"},
    "model_failed": {"ja": "モデルを取得できませんでした: {error}", "en": "Couldn't download the model: {error}"},
    "model_ready": {"ja": "モデルの準備ができました: {path}", "en": "The model is ready: {path}"},
}


def _t(key: str, **fields: object) -> str:
    from zundamon_jarvis import i18n

    return i18n.text(TEXT, key, **fields)


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
    if cmd == "tray" and not rest:
        from zundamon_jarvis.tray import run_tray

        return run_tray()
    if cmd == "supervise":
        from zundamon_jarvis import supervisor

        return supervisor.main(rest)
    if cmd == "service" and len(rest) == 1 and rest[0] in ("install", "uninstall", "status", "start", "stop"):
        from zundamon_jarvis import platform, service

        sys_ = platform.system()
        if rest[0] in ("start", "stop") and sys_ != "windows":
            how = "systemctl --user start|stop zundamon-jarvis" if sys_ == "linux" else "launchctl"
            print(f"service {rest[0]} is for Windows (the supervisor); here use {how}", file=sys.stderr)
            return 2
        if rest[0] == "start":
            print("started" if service.start_services(sys_) else "already running")
        elif rest[0] == "stop":
            print(service.stop_services(sys_))
        elif rest[0] == "install":
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

        print(_t("model_fetching"), flush=True)
        try:
            where = stt.download_model()
        except Exception as e:  # network, disk: say what happened, the installer stops here
            print(_t("model_failed", error=e), file=sys.stderr)
            return 1
        print(_t("model_ready", path=where))
        return 0
    if cmd == "setup":  # the web page; --cli: the terminal questions; --demo: the page with fakes
        from zundamon_jarvis import setup_gui

        return setup_gui.main(rest)
    if cmd == "doctor":
        from zundamon_jarvis import doctor

        return doctor.main(rest)
    if cmd == "update":
        from zundamon_jarvis import update

        return update.main(rest)
    if cmd == "uninstall":
        from zundamon_jarvis import setup_wizard

        return setup_wizard.uninstall_main(rest)
    print(USAGE, file=sys.stderr)
    return 2
