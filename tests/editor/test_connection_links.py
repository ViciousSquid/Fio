"""Regression tests for persistent 2D I/O connection links."""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtCore import QPointF, QRectF

from editor.io_system import OutputConnection
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


def test_io_link_uses_stable_target_id_and_survives_endpoint_culling(main_window):
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
    main_window.state.brushes[:] = [source, wrong_target, right_target]
    view = main_window.view_top
    view.view_type = 'top'
    painter = _Painter()
    view.draw_logic_connections(
        painter,
        QRectF(-100.0, -100.0, 200.0, 200.0),
    )
    assert len(painter.lines) == 1
    start, end = painter.lines[0]
    assert start != end



def test_connection_link_visibility_toggle_updates_shared_state(main_window):
    main_window.set_connection_links_enabled(False)
    assert main_window.show_logic_links is False

    main_window.set_connection_links_enabled(True)
    assert main_window.show_logic_links is True
