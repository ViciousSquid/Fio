"""A 2D gesture is one undo step, and Ctrl+Z takes it back the first time.

A drag, a brush handle resize and a drawn brush each take one checkpoint as
they begin. The release used to take a second checkpoint of the result, so
the first Ctrl+Z after a drag restored the scene to how it already was; a
resize, which had no checkpoint of its own before it changed anything, could
not be undone without undoing the step before it as well.
"""

import pytest

pytest.importorskip("PyQt5", reason="the 2D views are Qt widgets")

from PyQt5.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent           # noqa: E402

from tests.helpers.worlds import box_brush    # noqa: E402

pytestmark = pytest.mark.qt


def _mouse(view, kind, point, button, buttons):
    event = QMouseEvent(kind, QPointF(point), button, buttons, Qt.NoModifier)
    {QEvent.MouseButtonPress: view.mousePressEvent,
     QEvent.MouseMove: view.mouseMoveEvent,
     QEvent.MouseButtonRelease: view.mouseReleaseEvent}[kind](event)


def _gesture(view, start, end, steps=4):
    _mouse(view, QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    for i in range(1, steps + 1):
        p = QPointF(start.x() + (end.x() - start.x()) * i / steps,
                    start.y() + (end.y() - start.y()) * i / steps)
        _mouse(view, QEvent.MouseMove, p, Qt.NoButton, Qt.LeftButton)
    _mouse(view, QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton)


@pytest.fixture
def scene(main_window):
    window = main_window
    brush = box_brush("crate", (0, 0, 0), (64, 64, 64))
    window.state.brushes[:] = [brush]
    window.state.things[:] = []
    window.state.undo_stack.clear()
    window.state.redo_stack.clear()
    window.state.save_state()           # the entry a load leaves at the bottom
    view = window.view_top
    view.view_type = 'top'
    view.resize(400, 300)
    view.zoom_factor = 1.0
    view.pan_offset = QPointF(0.0, 0.0)
    window.set_tool_mode('select')
    return window, view, brush


def test_a_drag_is_one_undo_step_and_the_first_ctrl_z_takes_it_back(scene):
    window, view, brush = scene
    window.set_selected_objects([brush])
    start = view.world_to_screen(QPointF(0.0, 0.0))
    _gesture(view, start, QPointF(start.x() + 96, start.y()))
    moved = list(window.state.brushes[0]['pos'])
    assert moved != [0.0, 0.0, 0.0]
    assert len(window.state.undo_stack) == 2
    window.undo()
    assert list(window.state.brushes[0]['pos']) == [0.0, 0.0, 0.0]
    window.redo()
    assert list(window.state.brushes[0]['pos']) == moved


def _handle_point(view):
    corner = view.world_to_screen(QPointF(32.0, 32.0))   # on the grid: no snap
    if view.get_handle_at(corner) == -1:
        pytest.skip("no resize handle at the brush's corner")
    return corner


def test_a_resize_is_one_undo_step_and_a_handle_click_is_none(scene):
    window, view, brush = scene
    window.set_selected_objects([brush])
    handle = _handle_point(view)
    _gesture(view, handle, handle, steps=1)               # a click on the handle
    assert len(window.state.undo_stack) == 1
    _gesture(view, handle, QPointF(handle.x() + 80, handle.y() + 80))
    resized = list(window.state.brushes[0]['size'])
    assert resized != [64.0, 64.0, 64.0]
    assert len(window.state.undo_stack) == 2
    window.undo()
    assert list(window.state.brushes[0]['size']) == [64.0, 64.0, 64.0]
    assert list(window.state.brushes[0]['pos']) == [0.0, 0.0, 0.0]


def test_a_drawn_brush_is_one_undo_step(scene):
    window, view, brush = scene
    window.set_selected_objects([])
    window.set_tool_mode('brush')
    start = view.world_to_screen(QPointF(64.0, 64.0))
    _gesture(view, start, QPointF(start.x() + 96, start.y() + 96))
    assert len(window.state.brushes) == 2
    assert len(window.state.undo_stack) == 2
    window.undo()
    assert len(window.state.brushes) == 1
