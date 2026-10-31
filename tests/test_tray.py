import threading
import types

import pytest

from zundamon_jarvis import menubar, tray


class MenuItem:
    def __init__(self, text, action, enabled=True, default=False):
        # pystray refuses a callable that does not take exactly (icon, item)
        assert action is None or action.__code__.co_argcount == 2
        self.text, self.action, self.enabled, self.default = text, action, enabled, default


class Menu:
    SEPARATOR = MenuItem("- - - -", None)

    def __init__(self, *items):
        self.items = list(items)


class Icon:
    HAS_NOTIFICATION = True

    def __init__(self, name, icon=None, title=""):
        self.name, self.icon, self.title, self.menu = name, icon, title, None
        self.visible = False
        self.notes = []
        self.stopped = threading.Event()
        self.menu_sets = 0

    def __setattr__(self, key, value):
        if key == "menu" and value is not None:
            object.__setattr__(self, "menu_sets", self.menu_sets + 1)
        object.__setattr__(self, key, value)

    def notify(self, message, title=None):
        self.notes.append(message)

    def run(self, setup):
        t = threading.Thread(target=setup, args=(self,))
        t.start()
        self.stopped.wait(5)
        t.join(5)

    def stop(self):
        self.stopped.set()


fake_pystray = types.SimpleNamespace(MenuItem=MenuItem, Menu=Menu, Icon=Icon)


def texts(menu):
    return [(i.text, i.enabled, i.default) for i in menu.items]


def test_badge_per_state_and_none_when_down():
    assert tray.badge_color(None) is None
    colors = {tray.badge_color(s) for s in ("listening", "paused", "thinking", "speaking", "limit", "offline")}
    assert len(colors) == 6
    assert tray.badge_color("something-new") == tray.BADGE["offline"]


def test_tooltip():
    assert tray.tooltip(None) == "ZundamonJarvis: not running"
    assert tray.tooltip({"state": "listening", "text": "Listening"}) == "ZundamonJarvis: Listening"
    assert len(tray.tooltip({"state": "x", "text": "y" * 500})) == 127


def test_notes_are_wrapped_for_a_narrow_menu():
    lines = tray.note_lines(menubar.WIN_MIC_NOTE + "\n" + menubar.LOGIN_NOTE)
    assert all(len(line) <= tray.NOTE_WIDTH for line in lines)
    assert "".join(lines).replace(" ", "") == (menubar.WIN_MIC_NOTE + menubar.LOGIN_NOTE).replace(" ", "")
    assert tray.note_lines(None) == []


def test_menu_mirrors_the_menu_bar_and_left_click_opens_the_conversation():
    acted = []
    menu = tray.build_menu(fake_pystray, menubar.menu_model({"state": "listening", "text": "Listening"}, "windows"),
                           acted.append)
    assert texts(menu) == [
        ("Listening", False, False),
        ("Pause", True, False),
        ("Conversation…", True, True),
        ("Settings…", True, False),
        ("Quit tray (daemon keeps running)", True, False),
    ]
    for item in menu.items:
        if item.action:
            item.action(None, item)
    assert acted == ["pause", "conversation", "settings", "quit"]


def test_menu_when_down_has_no_default_and_notes_after_a_separator():
    menu = tray.build_menu(fake_pystray, menubar.menu_model(None, "windows"), lambda a: None)
    assert not any(i.default for i in menu.items)
    note = tray.build_menu(fake_pystray, menubar.menu_model(
        {"state": "offline", "text": "Offline (mic)"}, "windows"), lambda a: None)
    i = note.items.index(Menu.SEPARATOR)
    assert all(not it.enabled for it in note.items[i + 1:]) and len(note.items) > i + 1


def test_tray_app_updates_only_what_changed_and_notifies_once():
    icon = Icon("x")
    images = []
    app = tray.TrayApp(fake_pystray, icon, lambda a: None, image=lambda s: images.append(s) or f"img:{s}")
    app.show(None)
    app.show(None)
    assert images == [None] and icon.menu_sets == 1 and icon.title == "ZundamonJarvis: not running"
    app.show({"state": "listening", "text": "Listening"})
    assert icon.icon == "img:listening" and icon.menu_sets == 2
    off = {"state": "offline", "text": "Offline (mic)"}
    app.show(off)
    app.show(off)
    assert len(icon.notes) == 1 and "マイク" in icon.notes[0]
    assert images == [None, "listening", "offline"]


def test_act_on_quit_stops_and_others_run_off_thread(monkeypatch):
    opened, stopped = [], []
    done = threading.Event()
    monkeypatch.setattr(tray.ctl, "open_window", lambda name: (opened.append(name), done.set()))
    tray.act_on("quit", lambda: stopped.append(1))
    tray.act_on("settings", lambda: stopped.append(2))
    assert done.wait(5)
    assert stopped == [1] and opened == ["settings"]


class Lock:
    def __init__(self, free=True):
        self.free, self.released = free, False

    def acquire(self):
        return self.free

    def release(self):
        self.released = True


def test_run_tray_polls_until_quit(monkeypatch):
    monkeypatch.setattr(tray, "POLL_S", 0.01)
    polls = []
    holder = {}

    class QuittingIcon(Icon):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            holder["icon"] = self

    def fetch():
        polls.append(1)
        if len(polls) == 2:  # the user picks "Quit tray"
            item = holder["icon"].menu.items[-1]
            item.action(None, item)
        return {"state": "listening", "text": "Listening"}

    pystray = types.SimpleNamespace(MenuItem=MenuItem, Menu=Menu, Icon=QuittingIcon)
    lock = Lock()
    assert tray.run_tray(pystray, fetch=fetch, image=lambda s: s, lock=lock) == 0
    assert holder["icon"].visible and holder["icon"].stopped.is_set()
    assert len(polls) == 2 and lock.released


def test_a_second_tray_exits_at_once():
    assert tray.run_tray(types.SimpleNamespace(Icon=lambda *a, **k: pytest.fail("no icon")),
                         lock=Lock(free=False)) == 0


def test_icon_image_has_a_dot_and_greys_when_down():
    pytest.importorskip("PIL")
    up, down = tray.icon_image("listening"), tray.icon_image(None)
    assert up.size == down.size == (tray.ICON_SIZE, tray.ICON_SIZE)
    corner = up.getpixel((tray.ICON_SIZE - 6, tray.ICON_SIZE - 6))
    assert corner[:3] == tray.BADGE["listening"]
    r, g, b, _ = down.getpixel((tray.ICON_SIZE // 2, tray.ICON_SIZE // 2))
    assert r == g == b
