"""macOS menu bar app: `zundamon-jarvis menubar`.

`menu_model` is a pure function (status dict from the control socket, or None when the daemon is
not running -> what to draw); `run_menubar` draws it with rumps (macOS arm64 only, imported there).
Quitting closes only the menu bar app; the daemon (LaunchAgent) keeps running.

The Windows tray icon (``zundamon_jarvis.tray``) draws the same model: ``menu_model(status,
"windows")`` only swaps the quit label and the OS-specific microphone advice.

When a newer release is out (``zundamon_jarvis.update``, looked up in the background: a minute
after start, then every 6 h, cached), ``menu_model(..., update=tag)`` adds "Update to <tag>…" above
the quit item and a note; the item runs ``zundamon-jarvis update --yes`` detached.
"""

import threading
from dataclasses import dataclass, field

from zundamon_jarvis import ctl
from zundamon_jarvis import update as updater
from zundamon_jarvis.control import request

POLL_S = 2
REQUEST_TIMEOUT_S = 1.0
# The watch window's emoji icons (ctl.STATE_ICON), plus the two it leaves to the text.
TITLE_ICON = {**ctl.STATE_ICON, "thinking": "⏳", "speaking": "🔊"}
DOWN_ICON = "💤"

# The daemon is uv's python3.12 (a LaunchAgent), and that is the name macOS lists.
MIC_NOTE = ("マイクが使えません。システム設定 > プライバシーとセキュリティ > マイク で、"
            "「python3.12」をオンにしてください")
# Windows lists plain Win32 programs only under the "desktop apps" switch, not one by one.
WIN_MIC_NOTE = ("マイクが使えません。設定 > プライバシーとセキュリティ > マイク で、"
                "「デスクトップ アプリがマイクにアクセスできるようにする」をオンにしてください")
QUIT_LABEL = {"mac": "Quit menu bar (daemon keeps running)", "windows": "Quit tray (daemon keeps running)"}
LOGIN_NOTE = "Claude にログインしていません。ターミナルで `claude` を実行してログインしてください"


_UNSET = object()  # nothing polled yet


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str | None  # "pause" | "resume" | "conversation" | "settings" | "update" | "quit" | None (just a label)
    enabled: bool = True


@dataclass(frozen=True)
class MenuModel:
    title: str
    items: list[MenuItem] = field(default_factory=list)
    note: str | None = None


def with_update(model: MenuModel, tag: str, lang: str | None = None) -> MenuModel:
    """``model`` with "Update to <tag>…" above its last (quit) item, and a note saying so."""
    items = [*model.items[:-1], MenuItem(updater.menu_text("item", tag, lang), "update"), *model.items[-1:]]
    note = "\n".join(n for n in (model.note, updater.menu_text("note", tag, lang)) if n)
    return MenuModel(model.title, items, note)


def menu_model(status: dict | None, system: str = "mac", update: str | None = None) -> MenuModel:
    if update:  # the newer release's tag
        return with_update(menu_model(status, system), update)
    quit_item = MenuItem(QUIT_LABEL.get(system, QUIT_LABEL["mac"]), "quit")
    if status is None:
        return MenuModel(DOWN_ICON, [
            MenuItem("Not running", None, False),
            MenuItem("Pause", "pause", False),
            MenuItem("Conversation…", "conversation", False),
            MenuItem("Settings…", "settings", False),
            quit_item,
        ])
    state, text = status.get("state", ""), status.get("text", "")
    notes = []
    if state == "offline" and "mic" in text:
        notes.append(WIN_MIC_NOTE if system == "windows" else MIC_NOTE)
    if status.get("login") is False:
        notes.append(LOGIN_NOTE)
    return MenuModel(TITLE_ICON.get(state, "•"), [
        MenuItem(text or state, None, False),
        MenuItem("Resume", "resume") if state == "paused" else MenuItem("Pause", "pause"),
        MenuItem("Conversation…", "conversation"),
        MenuItem("Settings…", "settings"),
        quit_item,
    ], "\n".join(notes) or None)


def fetch_status() -> dict | None:
    try:
        resp = request("status", timeout=REQUEST_TIMEOUT_S)
    except (OSError, ValueError):
        return None
    return None if "error" in resp else resp


def run_menubar() -> int:
    import rumps  # macOS arm64 only

    app = rumps.App("ZundamonJarvis", title=DOWN_ICON, quit_button=None)
    shown: list[MenuModel | None] = [None]
    latest: list[object] = [_UNSET]  # written by the poll thread, read on the main thread
    newer: list[str | None] = [None]  # written by the update check's thread
    updater.start_watch(lambda tag: newer.__setitem__(0, tag))

    def act(action: str | None) -> None:
        def run() -> None:
            if action in ("pause", "resume"):
                try:
                    request(action, timeout=REQUEST_TIMEOUT_S)
                except (OSError, ValueError):
                    pass
            elif action in ("conversation", "settings"):
                ctl.open_window("watch" if action == "conversation" else "settings")
            elif action == "update":
                updater.launch_from_menu("mac")
        if action == "quit":
            rumps.quit_application()
        elif action:
            threading.Thread(target=run, daemon=True).start()  # never block the UI thread

    def render(model: MenuModel) -> None:
        if model == shown[0]:
            return
        shown[0] = model
        app.title = model.title
        app.menu.clear()
        for item in model.items:
            mi = rumps.MenuItem(item.label, callback=(lambda _s, a=item.action: act(a)) if item.enabled and item.action else None)
            app.menu.add(mi)
        if model.note:
            app.menu.add(None)
            for line in model.note.split("\n"):
                app.menu.add(rumps.MenuItem(line))

    def poll_once() -> None:
        latest[0] = fetch_status()

    def tick(_timer: object) -> None:
        if latest[0] is not _UNSET:
            render(menu_model(latest[0], update=newer[0]))  # type: ignore[arg-type]
        threading.Thread(target=poll_once, daemon=True).start()

    timer = rumps.Timer(tick, POLL_S)
    timer.start()
    render(menu_model(fetch_status()))  # first picture right away (1 s at most)
    app.run()
    return 0
