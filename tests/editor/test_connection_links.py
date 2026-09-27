"""Regression tests for persistent 2D I/O connection links."""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtCore import QPointF, QRectF

from editor.io_system import OutputConnection
from editor.main_window import MainWindow
from editor.view_2d import View2D


class _Painter:
    def __init__(self):
        self.lines = []

    def setPen(self, _pen):
        pass

    def setBrush(self, _brush):
        pass

    def drawLine(self, p1, p2):
        self.lines.append((p1, p2))


class _Config:
    def getboolean(self, _section, _option, fallback=False):
        return fallback


class _State:
    def __init__(self, brushes=(), things=()):
        self.brushes = list(brushes)
        self.things = list(things)


class _Editor:
    def __init__(self, state):
        self.state = state
        self.show_logic_links = True


class _MainWindow:
    def __init__(self):
        self.config = _Config()


def _view(editor, main_window):
    view = View2D.__new__(View2D)
    view.editor = editor
    view.main_window = main_window
    view.view_type = 'top'
    view.connection_animations = {}
    view.arrow_travel_progress = {}
    view.last_io_connections = set()
    view.last_patrol_connections = set()
    view.get_axes = lambda: ('x', 'z')
    view.world_to_screen = lambda p: QPointF(p.x(), p.y())
    view._draw_connection_arrow = lambda *args: None
    view._draw_traveling_arrows = lambda *args: None
    return view


def test_segment_visibility_keeps_a_long_link_crossing_the_view():
    visible = QRectF(-100.0, -100.0, 200.0, 200.0)

    assert View2D._segment_intersects_rect(
        QPointF(-10000.0, 0.0),
        QPointF(10000.0, 0.0),
        visible,
    )

    assert not View2D._segment_intersects_rect(
        QPointF(-10000.0, 1000.0),
        QPointF(10000.0, 1000.0),
        visible,
    )


def test_io_link_uses_stable_target_id_and_survives_endpoint_culling():
    source = {
        'id': 'source',
        'name': 'button',
        'pos': [-10000.0, 0.0, 0.0],
        '_io_connections': [
            OutputConnection(
                output_name='OnTrigger',
                target_name='door',
                input_name='Fire',
                target_id='target-b',
            )
        ],
    }
    wrong_target = {
        'id': 'target-a',
        'name': 'door',
        'pos': [-5000.0, 50.0, 0.0],
    }
    right_target = {
        'id': 'target-b',
        'name': 'door',
        'pos': [10000.0, 0.0, 0.0],
    }

    editor = _Editor(_State(
        brushes=[source, wrong_target, right_target],
    ))
    main_window = _MainWindow()
    view = _view(editor, main_window)
    painter = _Painter()

    view.draw_logic_connections(
        painter,
        QRectF(-100.0, -100.0, 200.0, 200.0),
    )

    assert len(painter.lines) == 1
    start, end = painter.lines[0]
    assert start == QPointF(-10000.0, 0.0)
    assert end == QPointF(10000.0, 0.0)


def test_connection_link_visibility_toggle_updates_shared_state():
    host = type('Host', (), {})()
    host.show_logic_links = True
    host.connection_links_action = None
    host.view_3d = type('View3D', (), {'update': lambda self: None})()
    host.toasts = []
    host.update_views = lambda: None
    host.show_toast = lambda message: host.toasts.append(message)

    MainWindow.set_connection_links_enabled(host, False)

    assert host.show_logic_links is False
    assert host.toasts[-1] == 'Connection Links: OFF'

    MainWindow.set_connection_links_enabled(host, True)

    assert host.show_logic_links is True
    assert host.toasts[-1] == 'Connection Links: ON'
