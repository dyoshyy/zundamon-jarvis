"""`zundamon-jarvisctl character`: a small animated character at the bottom-right of the screen.

GTK4 + gtk4-layer-shell + cairo + Pango (system packages: python-gobject, gtk4, gtk4-layer-shell,
python-cairo). The window is transparent, borderless and above everything; only the character
itself takes the pointer (the bubble area is click-through):

- drag it to move it (it stays where it is left: ``character.Prefs``),
- click it to open / close the conversation window (``zundamon-jarvisctl window watch``),
- right-click it to switch the speech bubble on / off (remembered too).

The state comes from the daemon's ``status`` command on the control socket (the same source as
the bar); the state -> animation mapping is ``zundamon_jarvis.character.pose_for``. The bubble
and the "I hear you" cue come from the control socket's ``watch`` stream
(``character.Bubble``; ``hearing`` events).

This module needs the *system* python (the venv has no ``gi``); ``zundamon-jarvisctl character`` starts it
that way (see ``zundamon_jarvis.ctl.run_character``). Without layer-shell support it falls back to a plain
window, which a Hyprland window rule can float and pin.
"""

import json
import math
import os
import socket
import subprocess
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

from zundamon_jarvis.character import (  # noqa: E402
    Bubble,
    Line,
    Pose,
    Prefs,
    clamp_margins,
    is_click,
    load_prefs,
    normalize,
    pose_for,
    prefs_path,
    save_prefs,
)
from zundamon_jarvis.control import request, socket_path  # noqa: E402

APP_ID = "dev.dyoshyy.zundamon_jarvis.character"
NAMESPACE = "zundamon-jarvis-character"
CHAR_W, CHAR_H = 160, 180  # the character, at the bottom-right of the window
BUBBLE_H = 110  # room above it for the speech bubble
WIDTH, HEIGHT = 300, CHAR_H + BUBBLE_H
CHAR_X, CHAR_Y = WIDTH - CHAR_W, HEIGHT - CHAR_H
POLL_S = 0.5
RETRY_S = 2.0
FPS_MS = 33
BUBBLE_FONT = "sans 11"
BUBBLE_PAD = 10
BUBBLE_LINES = 4
# (fill, text) per speaker
BUBBLE_STYLE = {"me": ((1.0, 1.0, 1.0, 0.96), (0.12, 0.14, 0.25)),
                "you": ((0.86, 0.96, 0.87, 0.96), (0.10, 0.30, 0.15)),
                "note": ((0.12, 0.14, 0.25, 0.88), (1.0, 1.0, 1.0))}

BODY = {"normal": (0.45, 0.75, 1.0), "dim": (0.40, 0.50, 0.70), "warm": (1.0, 0.70, 0.40),
        "grey": (0.60, 0.60, 0.62)}
INK = (0.12, 0.14, 0.25)


class StatePoller:
    """Polls the daemon's ``status`` on a thread; ``state`` is always safe to read."""

    def __init__(self) -> None:
        self.state = "offline"
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                self.state = normalize(request("status", timeout=1.0).get("state"))
            except (OSError, ValueError, AttributeError):
                self.state = "offline"
            time.sleep(POLL_S)


class EventStream:
    """Follows the control socket's ``watch`` stream on a thread; each event (and a
    ``{"kind": "hello"}`` on every disconnect, so nothing stale stays up) goes to ``deliver``
    on the GTK main loop."""

    def __init__(self, deliver) -> None:
        self._deliver = deliver
        threading.Thread(target=self._run, daemon=True).start()

    def _send(self, ev: dict) -> None:
        GLib.idle_add(lambda: self._deliver(ev) and False)

    def _run(self) -> None:
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2)
                    s.connect(str(socket_path()))
                    s.sendall(b"watch\n")
                    s.settimeout(None)
                    with s.makefile("rb") as f:
                        for raw in f:
                            try:
                                ev = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(ev, dict):
                                self._send(ev)
            except OSError:
                pass
            self._send({"kind": "hello"})
            time.sleep(RETRY_S)


def draw_character(cr: cairo.Context, w: float, h: float, p: Pose) -> None:
    r, g, b = BODY[p.tint]
    cr.save()
    cr.translate(w / 2, h * 0.92)  # origin at the feet
    unit = min(w, h * 0.8) / 2
    cr.rotate(p.sway)
    cr.translate(0, p.body_dy * unit * 2)
    cr.scale(1 - p.squash, 1 + p.squash)
    rx, ry, cy = unit * 0.85, unit * 0.8, -unit * 0.85

    cr.push_group()
    # antenna
    cr.set_source_rgb(*INK)
    cr.set_line_width(3)
    cr.move_to(0, cy - ry + 4)
    cr.curve_to(unit * 0.05, cy - ry - unit * 0.2, -unit * 0.05, cy - ry - unit * 0.25, 0,
                cy - ry - unit * 0.35)
    cr.stroke()
    if p.glow:  # hearing: the bulb lights up
        cr.arc(0, cy - ry - unit * 0.4, 11, 0, 2 * math.pi)
        cr.set_source_rgba(1.0, 0.9, 0.3, 0.45)
        cr.fill()
    cr.arc(0, cy - ry - unit * 0.4, 5, 0, 2 * math.pi)
    cr.set_source_rgb(1.0, 0.95, 0.5) if p.glow else cr.set_source_rgb(1.0, 0.85, 0.35)
    cr.fill()
    # feet
    cr.set_source_rgb(r * 0.7, g * 0.7, b * 0.7)
    for fx in (-unit * 0.35, unit * 0.35):
        cr.save()
        cr.translate(fx, -unit * 0.06)
        cr.scale(unit * 0.22, unit * 0.12)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    # body: a rounded blob
    cr.save()
    cr.translate(0, cy)
    cr.scale(rx, ry)
    cr.arc(0, 0, 1, 0, 2 * math.pi)
    cr.restore()
    cr.set_source_rgb(r, g, b)
    cr.fill_preserve()
    cr.set_source_rgb(r * 0.6, g * 0.6, b * 0.6)
    cr.set_line_width(3)
    cr.stroke()
    # cheeks
    cr.set_source_rgba(1.0, 0.55, 0.6, 0.5 if p.tint != "grey" else 0.0)
    for cx in (-rx * 0.62, rx * 0.62):
        cr.save()
        cr.translate(cx, cy + ry * 0.25)
        cr.scale(unit * 0.14, unit * 0.09)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    # eyes
    ey = cy - ry * 0.12
    for ex in (-rx * 0.38, rx * 0.38):
        cr.set_source_rgb(*INK)
        cr.set_line_width(3)
        if p.x_eyes:
            d = unit * 0.08
            cr.move_to(ex - d, ey - d); cr.line_to(ex + d, ey + d)
            cr.move_to(ex + d, ey - d); cr.line_to(ex - d, ey + d)
            cr.stroke()
        elif p.eye_open < 0.15:  # shut: a small arc
            cr.arc(ex, ey, unit * 0.09, 0.15 * math.pi, 0.85 * math.pi)
            cr.stroke()
        else:
            cr.save()
            cr.translate(ex, ey + p.eye_look_y * unit * 0.06)
            cr.scale(unit * 0.09, unit * 0.12 * p.eye_open)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.fill()
            cr.set_source_rgb(1, 1, 1)  # glint
            cr.arc(ex + unit * 0.03, ey - unit * 0.04 + p.eye_look_y * unit * 0.06, 2.2, 0,
                   2 * math.pi)
            cr.fill()
    # mouth
    my = cy + ry * 0.35
    cr.set_source_rgb(*INK)
    cr.set_line_width(3)
    if p.mouth_open > 0.08:
        cr.save()
        cr.translate(0, my)
        cr.scale(unit * 0.14, unit * (0.03 + 0.14 * p.mouth_open))
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.fill()
    elif p.mouth_flat:
        cr.move_to(-unit * 0.1, my + 2)
        cr.curve_to(-unit * 0.04, my - 3, unit * 0.04, my + 5, unit * 0.1, my)
        cr.stroke()
    else:
        cr.arc(0, my - unit * 0.06, unit * 0.12, 0.2 * math.pi, 0.8 * math.pi)
        cr.stroke()
    # sweat drop
    if p.sweat:
        cr.set_source_rgba(0.5, 0.8, 1.0, 0.9)
        sx, sy = rx * 0.75, cy - ry * 0.45
        cr.move_to(sx, sy - 9)
        cr.curve_to(sx + 7, sy + 2, sx + 5, sy + 9, sx, sy + 9)
        cr.curve_to(sx - 5, sy + 9, sx - 7, sy + 2, sx, sy - 9)
        cr.fill()
    # hearing: sound waves travel out from both sides of the head
    if p.waves >= 0:
        cr.set_line_width(3)
        for k in range(2):
            phase = (p.waves + k * 0.5) % 1.0
            d = rx + unit * 0.1 + phase * unit * 0.3
            cr.set_source_rgba(0.95, 0.55, 0.2, 0.9 * (1 - phase))
            for side in (-1, 1):
                start = -0.25 * math.pi if side > 0 else 0.75 * math.pi
                cr.new_sub_path()
                cr.arc(0, cy, d, start, start + 0.5 * math.pi)
                cr.stroke()
    cr.pop_group_to_source()
    cr.paint_with_alpha(p.alpha)
    # thinking dots and sleep z live in the unrotated frame, above the head
    cr.restore()
    top = h * 0.92 - unit * 2.2  # beside the antenna, inside the window
    if p.dots:
        cr.set_source_rgba(*INK, 0.8)
        for i in range(p.dots):
            cr.arc(w / 2 + 22 + i * 15, top, 5, 0, 2 * math.pi)
            cr.fill()
    if p.zzz >= 0:
        cr.set_source_rgba(0.85, 0.9, 1.0, 1 - p.zzz)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(16 + 10 * p.zzz)
        cr.move_to(w / 2 + 22 + 25 * p.zzz, top + 16 - 14 * p.zzz)
        cr.show_text("z")


def _rounded(cr: cairo.Context, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def draw_bubble(cr: cairo.Context, line: Line) -> None:
    """The bubble just above the character's head, its tail pointing at the head."""
    fill, ink = BUBBLE_STYLE[line.who]
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(BUBBLE_FONT))
    layout.set_width((WIDTH - 2 * BUBBLE_PAD - 8) * Pango.SCALE)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_height(-BUBBLE_LINES)
    layout.set_text(line.text, -1)
    _ink, logical = layout.get_pixel_extents()
    w = min(logical.width + 2 * BUBBLE_PAD, WIDTH - 4)
    h = min(logical.height + 2 * BUBBLE_PAD, BUBBLE_H + 2)
    x, bottom = WIDTH - 4 - w, CHAR_Y + 6
    y = bottom - h
    tail_x = min(max(CHAR_X + CHAR_W * 0.35, x + 18), x + w - 18)

    def outline() -> None:
        _rounded(cr, x, y, w, h, 12)
        cr.move_to(tail_x - 9, bottom - 1)
        cr.line_to(CHAR_X + CHAR_W * 0.38, CHAR_Y + 30)
        cr.line_to(tail_x + 9, bottom - 1)
        cr.close_path()

    # border first, then the fill over its inner half: no seam where the tail meets the box
    outline()
    cr.set_source_rgba(0.12, 0.14, 0.25, 0.3)
    cr.set_line_width(3)
    cr.stroke()
    outline()
    cr.set_source_rgba(*fill[:3], 1.0)
    cr.fill()
    cr.set_source_rgb(*ink)
    cr.move_to(x + BUBBLE_PAD - logical.x, y + BUBBLE_PAD)
    PangoCairo.show_layout(cr, layout)


def open_watch_window() -> None:
    """``zundamon-jarvisctl window watch`` (it toggles), without our GTK preload / PYTHONPATH."""
    ctl = os.environ.get("ZUNDAMON_JARVISCTL") or "zundamon-jarvisctl"
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_PRELOAD", "PYTHONPATH", "ZUNDAMON_JARVISCTL")}
    try:
        proc = subprocess.Popen([ctl, "window", "watch"], env=env, start_new_session=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        print(f"zundamon-jarvisctl character: could not open the watch window: {e}",
              file=sys.stderr)
        return
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it


def build_window(app: Gtk.Application, forced_state: str | None = None) -> Gtk.Window:
    poller = None if forced_state else StatePoller()
    bubble = Bubble()
    hearing = {"on": False}
    path = prefs_path()
    prefs = load_prefs(path)

    def on_event(ev: dict) -> None:
        if ev.get("kind") == "hearing":
            hearing["on"] = bool(ev.get("on"))
        elif ev.get("kind") == "hello":
            hearing["on"] = False
        bubble.feed(ev, time.monotonic())

    if not forced_state:
        EventStream(on_event)

    win = Gtk.ApplicationWindow(application=app)
    win.set_title("ZundamonJarvis character")
    win.set_decorated(False)
    win.set_default_size(WIDTH, HEIGHT)
    css = Gtk.CssProvider()
    css.load_from_string("window, window.background { background: transparent; }")
    Gtk.StyleContext.add_provider_for_display(
        win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    area = Gtk.DrawingArea()
    area.set_content_width(WIDTH)
    area.set_content_height(HEIGHT)
    area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
    start = time.monotonic()

    def on_draw(_a, cr, _w, _h) -> None:
        now = time.monotonic()
        state = ("listening" if forced_state == "hearing" else forced_state) or poller.state
        line = bubble.show(state, now)
        if line is not None and (prefs.bubble or line.who == "note"):
            draw_bubble(cr, line)
        cr.save()
        cr.translate(CHAR_X, CHAR_Y)
        draw_character(cr, CHAR_W, CHAR_H,
                       pose_for(state, now - start, hearing=hearing["on"] or forced_state == "hearing"))
        cr.restore()

    area.set_draw_func(on_draw)
    win.set_child(area)
    GLib.timeout_add(FPS_MS, lambda: (area.queue_draw(), True)[1])

    def input_on_character(_w) -> None:
        surface = win.get_surface()
        if surface is not None:  # everything but the character is click-through
            surface.set_input_region(cairo.Region(cairo.RectangleInt(CHAR_X, CHAR_Y, CHAR_W, CHAR_H)))

    win.connect("realize", input_on_character)

    layered = LayerShell is not None and LayerShell.is_supported()
    if layered:
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, NAMESPACE)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(win, -1)
        for edge in (LayerShell.Edge.BOTTOM, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)

    def screen() -> tuple[int, int] | None:
        surface = win.get_surface()
        monitor = surface and win.get_display().get_monitor_at_surface(surface)
        if not monitor:
            return None
        geo = monitor.get_geometry()
        return geo.width, geo.height

    def place(right: float, bottom: float) -> tuple[int, int]:
        size = screen()
        r, b = clamp_margins(right, bottom, (WIDTH, HEIGHT), size) if size else (int(right), int(bottom))
        if layered:
            LayerShell.set_margin(win, LayerShell.Edge.RIGHT, r)
            LayerShell.set_margin(win, LayerShell.Edge.BOTTOM, b)
        return r, b

    place(prefs.right, prefs.bottom)
    win.connect("map", lambda _w: place(prefs.right, prefs.bottom))  # clamp once the monitor is known

    # drag = move, click = watch window. The offsets are relative to the widget, which moves
    # with the window: the pointer's total movement is the offset plus how far it has moved.
    drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
    moving = {"start": (0, 0), "moved": (0, 0), "dragged": False}

    def total(ox: float, oy: float) -> tuple[float, float]:
        return ox + moving["moved"][0], oy + moving["moved"][1]

    def on_begin(_g, _x, _y) -> None:
        moving.update(start=(prefs.right, prefs.bottom), moved=(0, 0), dragged=False)

    def on_update(_g, ox: float, oy: float) -> None:
        dx, dy = total(ox, oy)
        if not moving["dragged"] and is_click(dx, dy):
            return
        if not moving["dragged"]:
            moving["dragged"] = True
            area.set_cursor(Gdk.Cursor.new_from_name("grabbing"))
        r0, b0 = moving["start"]
        r, b = place(r0 - dx, b0 - dy)
        moving["moved"] = (r0 - r, b0 - b)

    def on_end(_g, ox: float, oy: float) -> None:
        nonlocal prefs
        area.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        if not moving["dragged"]:
            open_watch_window()
            return
        r0, b0 = moving["start"]
        mx, my = moving["moved"]
        prefs = Prefs(right=int(r0 - mx), bottom=int(b0 - my), bubble=prefs.bubble)
        save_prefs(path, prefs)

    drag.connect("drag-begin", on_begin)
    drag.connect("drag-update", on_update)
    drag.connect("drag-end", on_end)
    area.add_controller(drag)

    right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)

    def on_right_click(_g, _n, _x, _y) -> None:
        nonlocal prefs
        prefs = Prefs(right=prefs.right, bottom=prefs.bottom, bubble=not prefs.bubble)
        save_prefs(path, prefs)
        bubble.note(f"Speech bubble: {'on' if prefs.bubble else 'off'}", time.monotonic())

    right_click.connect("released", on_right_click)
    area.add_controller(right_click)
    return win


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    # preview a state (or "hearing": listening with the hearing cue)
    forced = args[0] if args and (normalize(args[0]) == args[0] or args[0] == "hearing") else None
    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", lambda a: build_window(a, forced).present())
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
