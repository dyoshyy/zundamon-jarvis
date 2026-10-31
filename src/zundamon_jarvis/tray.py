"""Windows notification-area (tray) icon: ``zundamon-jarvis tray``, the macOS menu bar's counterpart.

It draws the menu bar's pure model (``menubar.menu_model(status, "windows")``): the state, Pause /
Resume, Conversation…, Settings…, Quit tray, in the display language (read on every poll). pystray (Windows only, imported in ``run_tray``)
shows it; the icon is ``assets/icon.png`` with a coloured dot for the state, greyed out while the
daemon is down. A left click opens the conversation (the menu's default item), as a click on
the Linux character does. The status is polled every 2 s on pystray's setup thread, never on
the UI thread, and the menu is rebuilt only when the model changes (rebuilding replaces the
native menu, which could be open at that moment).

The supervisor starts it at login. "Quit tray" exits 0, which the supervisor leaves quit (the
daemon keeps running); a crash restarts it. ``tray.lock`` in ``platform.runtime_dir()`` keeps
it to one icon when it is also started by hand.
"""

import textwrap
import threading
from collections.abc import Callable
from importlib import resources
from typing import Any

from zundamon_jarvis import ctl, i18n, menubar, platform
from zundamon_jarvis.control import request, status_label
from zundamon_jarvis.menubar import MenuModel

NAME = "ZundamonJarvis"
POLL_S = menubar.POLL_S
ICON_SIZE = 64
NOTE_WIDTH = 30  # characters per menu line: a Win32 menu is as wide as its longest item
LOCK = "tray.lock"
TEXT = {"not_running": {"ja": "停止中", "en": "not running"}}

BADGE = {  # RGB of the state dot
    "listening": (46, 160, 67),
    "thinking": (230, 162, 0),
    "speaking": (30, 120, 220),
    "paused": (128, 128, 128),
    "limit": (230, 110, 20),
    "offline": (210, 40, 40),
}

_UNSET = object()


def badge_color(state: str | None) -> tuple[int, int, int] | None:
    """None: no dot (the daemon is down, the icon is greyed instead)."""
    if state is None:
        return None
    return BADGE.get(state, BADGE["offline"])


def tooltip(status: dict | None, lang: str | None = None) -> str:
    """The hover text (Windows keeps 127 characters of it), in ``lang`` (default: the display
    language)."""
    if status is None:
        return f"{NAME}: {i18n.text(TEXT, 'not_running', lang)}"
    return f"{NAME}: {status_label(status, lang) or status.get('state') or '?'}"[:127]


def note_lines(note: str | None, width: int = NOTE_WIDTH) -> list[str]:
    lines: list[str] = []
    for para in (note or "").split("\n"):
        lines.extend(textwrap.wrap(para, width) or [])
    return lines


def icon_image(state: str | None, size: int = ICON_SIZE) -> Any:
    """The tray picture for ``state`` (None = daemon down) as a PIL image."""
    from PIL import Image, ImageDraw, ImageOps

    with resources.as_file(resources.files("zundamon_jarvis") / "assets" / "icon.png") as path:
        img = Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    color = badge_color(state)
    if color is None:
        grey = ImageOps.grayscale(img).convert("RGBA")
        grey.putalpha(img.getchannel("A").point(lambda a: a * 3 // 5))
        return grey
    r = size * 3 // 16
    box = (size - 2 * r - 1, size - 2 * r - 1, size - 1, size - 1)
    ImageDraw.Draw(img).ellipse(box, fill=(*color, 255), outline=(255, 255, 255, 255), width=max(1, size // 32))
    return img


def _callback(act: Callable[[str], None], action: str) -> Callable[[Any, Any], None]:
    # exactly (icon, item): pystray counts the parameters and refuses a default-argument lambda
    def on_click(_icon: Any, _item: Any) -> None:
        act(action)
    return on_click


def build_menu(pystray: Any, model: MenuModel, act: Callable[[str], None]) -> Any:
    items = []
    for it in model.items:
        enabled = it.enabled and it.action is not None
        callback = _callback(act, it.action) if enabled and it.action else None
        items.append(pystray.MenuItem(it.label, callback, enabled=enabled,
                                      default=enabled and it.action == "conversation"))
    lines = note_lines(model.note)
    if lines:
        items.append(pystray.Menu.SEPARATOR)
        items.extend(pystray.MenuItem(line, None, enabled=False) for line in lines)
    return pystray.Menu(*items)


class TrayApp:
    """Keeps a pystray icon in step with the daemon's status (``show`` is called per poll)."""

    def __init__(self, pystray: Any, icon: Any, act: Callable[[str], None],
                 image: Callable[[str | None], Any] = icon_image) -> None:
        self._pystray, self._icon, self._act, self._image = pystray, icon, act, image
        self._state: object = _UNSET
        self._model: MenuModel | None = None
        self._note: str | None = None

    def show(self, status: dict | None) -> None:
        state = None if status is None else status.get("state", "")
        if state != self._state:
            self._icon.icon = self._image(state)
            self._state = state
        lang = i18n.language()  # read per poll: a change in the settings window shows here
        self._icon.title = tooltip(status, lang)  # pystray skips an unchanged title
        model = menubar.menu_model(status, "windows", lang=lang)
        if model != self._model:
            self._icon.menu = build_menu(self._pystray, model, self._act)
            self._model = model
        if model.note and model.note != self._note and getattr(self._icon, "HAS_NOTIFICATION", False):
            self._icon.notify(model.note, NAME)  # the menu is hidden; say it once out loud
        self._note = model.note


def act_on(action: str, stop: Callable[[], None]) -> None:
    """A menu action. Called on the UI thread, so anything that waits runs on its own thread."""
    if action == "quit":
        stop()
        return

    def run() -> None:
        if action in ("pause", "resume"):
            try:
                request(action, timeout=menubar.REQUEST_TIMEOUT_S)
            except (OSError, ValueError):
                pass
        elif action in ("conversation", "settings"):
            ctl.open_window("watch" if action == "conversation" else "settings")

    threading.Thread(target=run, daemon=True).start()


def run_tray(pystray: Any = None, fetch: Callable[[], dict | None] = menubar.fetch_status,
             image: Callable[[str | None], Any] = icon_image, lock: Any = None) -> int:
    from zundamon_jarvis.supervisor import InstanceLock

    lock = lock or InstanceLock(platform.runtime_dir() / LOCK)
    if not lock.acquire():
        return 0  # another tray icon is up
    try:
        if pystray is None:
            import pystray  # Windows only
        stop = threading.Event()
        icon = pystray.Icon(NAME, icon=image(None), title=tooltip(None))

        def quit_tray() -> None:
            stop.set()  # first: icon.stop() waits for the poll thread to finish
            icon.stop()

        app = TrayApp(pystray, icon, lambda a: act_on(a, quit_tray), image)
        app.show(None)

        def poll(icon: Any) -> None:  # pystray's setup thread
            icon.visible = True
            while not stop.is_set():
                app.show(fetch())
                stop.wait(POLL_S)

        try:
            icon.run(poll)
        finally:
            stop.set()
    finally:
        lock.release()
    return 0
