"""`zundamon-jarvisctl character`: a small animated character at the bottom-right of the screen.

GTK4 + gtk4-layer-shell + cairo (system packages: python-gobject, gtk4, gtk4-layer-shell,
python-cairo). The window is transparent, borderless, above everything and click-through. The state
comes from the daemon's ``status`` command on the control socket (the same source as the bar);
the state -> animation mapping is ``zundamon_jarvis.character.pose_for``.

This module needs the *system* python (the venv has no ``gi``); ``zundamon-jarvisctl character`` starts it
that way (see ``zundamon_jarvis.ctl.run_character``). Without layer-shell support it falls back to a plain
window, which a Hyprland window rule can float and pin.
"""

import math
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402
except (ValueError, ImportError):
    LayerShell = None

from zundamon_jarvis.character import Pose, normalize, pose_for  # noqa: E402
from zundamon_jarvis.control import request  # noqa: E402

APP_ID = "dev.dyoshyy.zundamon_jarvis.character"
NAMESPACE = "zundamon-jarvis-character"
WIDTH, HEIGHT = 160, 180
MARGIN = 16
POLL_S = 0.5
FPS_MS = 33

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
    cr.arc(0, cy - ry - unit * 0.4, 5, 0, 2 * math.pi)
    cr.set_source_rgb(1.0, 0.85, 0.35)
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


def build_window(app: Gtk.Application, forced_state: str | None = None) -> Gtk.Window:
    poller = None if forced_state else StatePoller()
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
    start = time.monotonic()

    def on_draw(_a, cr, w, h) -> None:
        state = forced_state or poller.state
        draw_character(cr, w, h, pose_for(state, time.monotonic() - start))

    area.set_draw_func(on_draw)
    win.set_child(area)
    GLib.timeout_add(FPS_MS, lambda: (area.queue_draw(), True)[1])

    def click_through(_w) -> None:
        surface = win.get_surface()
        if surface is not None:
            surface.set_input_region(cairo.Region())

    win.connect("realize", click_through)

    if LayerShell is not None and LayerShell.is_supported():
        LayerShell.init_for_window(win)
        LayerShell.set_namespace(win, NAMESPACE)
        LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
        for edge in (LayerShell.Edge.BOTTOM, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(win, edge, True)
            LayerShell.set_margin(win, edge, MARGIN)
        LayerShell.set_exclusive_zone(win, -1)
    return win


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    forced = args[0] if args and normalize(args[0]) == args[0] else None  # preview a state
    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", lambda a: build_window(a, forced).present())
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
