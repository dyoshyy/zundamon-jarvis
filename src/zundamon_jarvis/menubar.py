"""macOS menu bar app: `zundamon-jarvis menubar`.

`menu_model` is a pure function (status dict from the control socket, or None when the daemon is
not running -> what to draw); `run_menubar` draws it with rumps (macOS arm64 only, imported there).
Quitting closes only the menu bar app; the daemon (LaunchAgent) keeps running.

The Windows tray icon (``zundamon_jarvis.tray``) draws the same model: ``menu_model(status,
"windows")`` only swaps the quit label and the OS-specific microphone advice. Labels are in the
display language (``zundamon_jarvis.i18n``), read on every draw: a change in the settings
window shows within one poll.
"""

import threading
from dataclasses import dataclass, field

from zundamon_jarvis import ctl, i18n
from zundamon_jarvis.control import request, status_label

POLL_S = 2
REQUEST_TIMEOUT_S = 1.0
# The watch window's emoji icons (ctl.STATE_ICON), plus the two it leaves to the text.
TITLE_ICON = {**ctl.STATE_ICON, "thinking": "⏳", "speaking": "🔊"}
DOWN_ICON = "💤"

TEXT = {
    "not_running": {"ja": "停止中", "en": "Not running"},
    "pause": {"ja": "一時停止", "en": "Pause"},
    "resume": {"ja": "再開", "en": "Resume"},
    "conversation": {"ja": "会話…", "en": "Conversation…"},
    "settings": {"ja": "設定…", "en": "Settings…"},
    "quit.mac": {"ja": "メニューバーを終了（本体は動き続けます）", "en": "Quit menu bar (daemon keeps running)"},
    "quit.windows": {"ja": "トレイを終了（本体は動き続けます）", "en": "Quit tray (daemon keeps running)"},
    # The daemon is uv's python3.12 (a LaunchAgent), and that is the name macOS lists.
    "mic.mac": {
        "ja": "マイクが使えません。システム設定 > プライバシーとセキュリティ > マイク で、「python3.12」をオンにしてください",
        "en": "The mic is blocked. Turn on “python3.12” in System Settings > Privacy & Security > Microphone",
    },
    # Windows lists plain Win32 programs only under the "desktop apps" switch, not one by one.
    "mic.windows": {
        "ja": ("マイクが使えません。設定 > プライバシーとセキュリティ > マイク で、"
               "「デスクトップ アプリがマイクにアクセスできるようにする」をオンにしてください"),
        "en": ("The mic is blocked. In Settings > Privacy & security > Microphone, "
               "turn on “Let desktop apps access your microphone”"),
    },
    "login": {
        "ja": "Claude にログインしていません。ターミナルで `claude` を実行してログインしてください",
        "en": "Not signed in to Claude. Run `claude` in a terminal to sign in",
    },
}


def _mic_blocked(status: dict) -> bool:
    if status.get("state") != "offline":
        return False
    reasons = status.get("reasons")
    if isinstance(reasons, list):
        return "mic" in reasons
    return "mic" in str(status.get("text", ""))  # a daemon from before "reasons"


_UNSET = object()  # nothing polled yet


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: str | None  # "pause" | "resume" | "conversation" | "settings" | "quit" | None (just a label)
    enabled: bool = True


@dataclass(frozen=True)
class MenuModel:
    title: str
    items: list[MenuItem] = field(default_factory=list)
    note: str | None = None


def menu_model(status: dict | None, system: str = "mac", lang: str | None = None) -> MenuModel:
    """What to draw for ``status``, labelled in ``lang`` (default: the display language now)."""
    lang = lang or i18n.language()

    def t(key: str) -> str:
        return i18n.text(TEXT, key, lang)

    os_key = "windows" if system == "windows" else "mac"
    quit_item = MenuItem(t(f"quit.{os_key}"), "quit")
    if status is None:
        return MenuModel(DOWN_ICON, [
            MenuItem(t("not_running"), None, False),
            MenuItem(t("pause"), "pause", False),
            MenuItem(t("conversation"), "conversation", False),
            MenuItem(t("settings"), "settings", False),
            quit_item,
        ])
    state = status.get("state", "")
    notes = []
    if _mic_blocked(status):
        notes.append(t(f"mic.{os_key}"))
    if status.get("login") is False:
        notes.append(t("login"))
    return MenuModel(TITLE_ICON.get(state, "•"), [
        MenuItem(status_label(status, lang) or state, None, False),
        MenuItem(t("resume"), "resume") if state == "paused" else MenuItem(t("pause"), "pause"),
        MenuItem(t("conversation"), "conversation"),
        MenuItem(t("settings"), "settings"),
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

    def act(action: str | None) -> None:
        def run() -> None:
            if action in ("pause", "resume"):
                try:
                    request(action, timeout=REQUEST_TIMEOUT_S)
                except (OSError, ValueError):
                    pass
            elif action in ("conversation", "settings"):
                ctl.open_window("watch" if action == "conversation" else "settings")
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
            render(menu_model(latest[0]))  # type: ignore[arg-type]
        threading.Thread(target=poll_once, daemon=True).start()

    timer = rumps.Timer(tick, POLL_S)
    timer.start()
    render(menu_model(fetch_status()))  # first picture right away (1 s at most)
    app.run()
    return 0
