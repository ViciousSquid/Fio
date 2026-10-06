"""Turning terrain sculpting on paints before the mouse has moved."""

import pytest

pytest.importorskip("PyQt5", reason="the 3D view is editor-tier")

pytestmark = pytest.mark.qt


def test_sculpting_can_start_before_the_mouse_moves(main_window, qt_app):
    """The brush cursor's position was only created by a mouse event, so the
    paint that activation itself requests raised in paintGL every frame until
    the mouse crossed the view."""
    view = main_window.view_3d

    view.set_terrain_sculpt_active(True)
    try:
        view.repaint()
        qt_app.processEvents()
        assert view._terrain_brush_hit is None
    finally:
        view.set_terrain_sculpt_active(False)
