"""Offscreen Qt tests for the floating-window manager and lazy property tabs.

Run with QT_QPA_PLATFORM=offscreen (set below before Qt is imported).
"""

import os
import sys

# Only when there is no display: the offscreen plugin cannot create an
# OpenGL context, and forcing it here would disable the visual tier for
# the whole session when the suite is run under Xvfb.
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

PyQt5 = pytest.importorskip("PyQt5")

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QPainter, QPixmap  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel, QTabWidget, QWidget  # noqa: E402

from engine.floating_windows import (  # noqa: E402
    CallbackWindow, FloatingWindow, WindowManager,
)

# Qt tier: PyQt5 must be importable.  No display and no GPU - the suite runs
# against the offscreen platform plugin.
pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance() or QApplication(sys.argv)
    yield inst


class _FakeEvent:
    """Stands in for a QMouseEvent (only pos/x/y are used)."""

    def __init__(self, x, y):
        self._p = QPoint(x, y)

    def pos(self):
        return self._p

    def x(self):
        return self._p.x()

    def y(self):
        return self._p.y()


# ---------------------------------------------------------------------------
# FloatingWindow
# ---------------------------------------------------------------------------

def test_window_starts_active_and_expanded(app):
    w = FloatingWindow("Test", x=10, y=10, width=300, body_height=100)
    assert w.active and w.expanded
    assert w._full_rect().height() == w.HEADER_H + 100


def test_collapse_toggle_changes_height(app):
    w = FloatingWindow("Test", x=0, y=0, width=300, body_height=120)
    rect = w._full_rect()
    # The collapse arrow sits in the 25px band left of the close button.
    w.handle_mouse_press(_FakeEvent(rect.right() - 35, rect.y() + 5))
    assert not w.expanded
    assert w._full_rect().height() == w.HEADER_H
    w.handle_mouse_press(_FakeEvent(rect.right() - 35, rect.y() + 5))
    assert w.expanded


def test_close_button_deactivates_and_fires_hook(app):
    fired = []
    w = FloatingWindow("Test", x=0, y=0, width=300)
    w.on_close = lambda: fired.append(True)
    rect = w._full_rect()
    consumed = w.handle_mouse_press(_FakeEvent(rect.right() - 10, rect.y() + 5))
    assert consumed and not w.active and fired == [True]


def test_minimum_width_is_enforced(app):
    w = FloatingWindow("Test", width=10)
    assert w.width == FloatingWindow.MIN_W


def test_drag_moves_and_clamps_to_the_view(app):
    w = FloatingWindow("Test", x=50, y=50, width=200, body_height=80)
    w.handle_mouse_press(_FakeEvent(100, 55))       # title bar
    assert w.dragging
    w.handle_mouse_move(_FakeEvent(400, 300), 800, 600)
    assert w.window_rect.x() > 50
    # Dragging far off-screen must clamp, never escape.
    w.handle_mouse_move(_FakeEvent(100000, 100000), 800, 600)
    assert w.window_rect.x() <= 800 - w.width - 5
    assert w.window_rect.y() <= 600 - w.HEADER_H - 5
    w.handle_mouse_release(_FakeEvent(0, 0))
    assert not w.dragging


def test_body_click_is_swallowed_not_leaked(app):
    w = FloatingWindow("Test", x=0, y=0, width=300, body_height=200)
    consumed = w.handle_mouse_press(_FakeEvent(50, w.HEADER_H + 40))
    assert consumed is True


def test_press_outside_is_not_consumed(app):
    w = FloatingWindow("Test", x=0, y=0, width=100, body_height=50)
    assert w.handle_mouse_press(_FakeEvent(5000, 5000)) is False


def test_a_failing_on_close_hook_does_not_propagate(app):
    w = FloatingWindow("Test", x=0, y=0, width=300)

    def boom():
        raise RuntimeError("hook exploded")

    w.on_close = boom
    rect = w._full_rect()
    w.handle_mouse_press(_FakeEvent(rect.right() - 10, rect.y() + 5))
    assert not w.active


# ---------------------------------------------------------------------------
# WindowManager
# ---------------------------------------------------------------------------

def test_manager_add_is_idempotent_and_raises(app):
    m = WindowManager()
    a, b = FloatingWindow("A"), FloatingWindow("B")
    m.add(a)
    m.add(b)
    assert m.windows == [a, b]
    m.add(a)                       # re-adding raises instead of duplicating
    assert m.windows == [a, b][::-1] or m.windows[-1] is a
    assert len(m.windows) == 2


def test_topmost_window_receives_the_click_and_is_raised(app):
    m = WindowManager()
    a = FloatingWindow("A", x=0, y=0, width=300, body_height=100)
    b = FloatingWindow("B", x=0, y=0, width=300, body_height=100)
    m.add(a)
    m.add(b)
    assert m.windows[-1] is b
    m.handle_mouse_press(_FakeEvent(150, 50))
    assert m.windows[-1] is b       # b was on top, stays on top
    # Now click where only a overlaps: move b away first.
    b.window_rect.moveTo(2000, 2000)
    m.handle_mouse_press(_FakeEvent(150, 50))
    assert m.windows[-1] is a       # a got the click and was raised


def test_prune_drops_closed_windows(app):
    m = WindowManager()
    a, b = FloatingWindow("A"), FloatingWindow("B")
    m.add(a)
    m.add(b)
    a.active = False
    m.prune()
    assert m.windows == [b]


def test_draw_all_survives_a_broken_window(app):
    class Broken(FloatingWindow):
        def draw(self, painter, focused=False):
            raise RuntimeError("bad paint")

    m = WindowManager()
    broken = Broken("Broken")
    good = FloatingWindow("Good")
    m.add(broken)
    m.add(good)

    pix = QPixmap(400, 400)
    painter = QPainter(pix)
    try:
        m.draw_all(painter)   # must not raise
    finally:
        painter.end()
    assert len(m.windows) == 2


def test_clear_and_find(app):
    m = WindowManager()
    a = FloatingWindow("Alpha")
    m.add(a)
    assert m.find(lambda w: w.title == "Alpha") is a
    assert m.find(lambda w: w.title == "Nope") is None
    m.clear()
    assert m.windows == []


# ---------------------------------------------------------------------------
# CallbackWindow
# ---------------------------------------------------------------------------

def test_callback_window_body_draw_and_click(app):
    drawn, clicked = [], []
    w = CallbackWindow(
        "key1", "Callback",
        draw_fn=lambda p, x, y, ww, hh: drawn.append((x, y, ww, hh)),
        on_body_click_cb=lambda x, y: clicked.append((x, y)) or True,
        width=300, body_height=120, x=0, y=0)

    pix = QPixmap(400, 400)
    painter = QPainter(pix)
    try:
        w.draw(painter)
    finally:
        painter.end()
    assert len(drawn) == 1

    assert w.handle_body_click(20, 60) is True
    assert clicked == [(20, 60)]


def test_callback_window_survives_a_failing_draw_fn(app):
    def boom(*_a):
        raise RuntimeError("draw exploded")

    w = CallbackWindow("k", "T", draw_fn=boom, width=300, body_height=100)
    pix = QPixmap(400, 400)
    painter = QPainter(pix)
    try:
        w.draw(painter)   # must not raise
    finally:
        painter.end()


def test_callback_window_wants_cursor_flag(app):
    assert CallbackWindow("k", "T", lambda *a: None).wants_cursor is False
    assert CallbackWindow("k", "T", lambda *a: None,
                          wants_cursor=True).wants_cursor is True


def test_no_rpg_inspector_window_was_ported():
    """The NPC mental-state window must NOT exist in generic Fio."""
    import engine.floating_windows as fw
    assert not hasattr(fw, "NpcDebugWindow")
    src = open(fw.__file__, encoding="utf-8").read()
    for banned in ("npc", "monster", "quest", "faction", "disposition"):
        assert banned not in src.lower(), f"RPG term '{banned}' leaked in"


# ---------------------------------------------------------------------------
# Lazy property tabs
# ---------------------------------------------------------------------------

def test_lazy_tab_factories_run_only_when_the_tab_is_shown(app):
    """Reproduces the lazy-tab mechanism from plugins.integration."""
    built = []

    def factory(thing):
        built.append(thing)
        return QLabel("heavy content")

    tabs = [("Tab A", factory), ("Tab B", factory)]
    widget = QTabWidget()
    widget.addTab(QLabel("stock"), "Properties")

    pending = {}
    for label, fac in tabs:
        placeholder = QWidget()
        from PyQt5.QtWidgets import QVBoxLayout
        lay = QVBoxLayout(placeholder)
        lay.setContentsMargins(0, 0, 0, 0)
        idx = widget.addTab(placeholder, label)
        pending[idx] = (placeholder, fac)
    widget._fio_pending_tabs = pending

    def _build_pending(index, w=widget, th="thing"):
        p = getattr(w, "_fio_pending_tabs", None)
        if not p or index not in p:
            return
        placeholder, fac = p.pop(index)
        inner = fac(th)
        if inner is not None:
            placeholder.layout().addWidget(inner)

    widget.currentChanged.connect(_build_pending)
    _build_pending(widget.currentIndex())

    # Current tab is the stock one, so no custom factory has run yet.
    assert built == []

    widget.setCurrentIndex(1)
    assert len(built) == 1

    # Re-selecting must not rebuild.
    widget.setCurrentIndex(0)
    widget.setCurrentIndex(1)
    assert len(built) == 1

    widget.setCurrentIndex(2)
    assert len(built) == 2


def test_integration_uses_a_namespaced_pending_attribute():
    here = os.path.dirname(__file__)
    src = open(os.path.join(here, "..", "..", "plugins", "integration.py"),
               encoding="utf-8").read()
    assert "_fio_pending_tabs" in src
    assert "_mw_pending_tabs" not in src, "MiniWind-namespaced attribute leaked in"
