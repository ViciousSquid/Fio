"""Collapsing the hierarchy preserves its contents and saves a reachable strip."""

import pytest

pytest.importorskip("PyQt5")
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest

pytestmark = pytest.mark.qt


def test_the_arrow_collapses_to_the_left_edge_and_restores_the_width(main_window, qt_app):
    window = main_window
    dock = window.scene_hierarchy_dock
    window.resizeDocks([dock], [230], Qt.Horizontal)
    qt_app.processEvents()
    width = dock.width()
    tree = window.scene_hierarchy.tree
    window.scene_hierarchy.search_box.setText("Village")

    QTest.mouseClick(dock.toggle_button, Qt.LeftButton)
    qt_app.processEvents()
    assert dock.collapsed and dock.width() == dock.STRIP_WIDTH
    assert dock.isVisible() and not window.scene_hierarchy.isVisible()
    assert window.dockWidgetArea(dock) == Qt.LeftDockWidgetArea
    assert dock.toggle_button.isVisible() and dock.toggle_button.arrowType() == Qt.RightArrow

    QTest.mouseClick(dock.toggle_button, Qt.LeftButton)
    qt_app.processEvents()
    assert not dock.collapsed and window.scene_hierarchy.isVisible()
    assert dock.width() == pytest.approx(width, abs=2)
    assert window.scene_hierarchy.tree is tree
    assert window.scene_hierarchy.search_box.text() == "Village"


def test_a_collapsed_hierarchy_is_saved_and_restored(main_window, qt_app):
    dock = main_window.scene_hierarchy_dock
    dock.set_collapsed(True)
    main_window.save_layout()
    dock.set_collapsed(False)
    main_window.load_layout()
    qt_app.processEvents()
    assert dock.collapsed and dock.width() == dock.STRIP_WIDTH
    assert dock.toggle_button.isVisible()


def test_reset_layout_restores_the_expanded_hierarchy(main_window, qt_app):
    dock = main_window.scene_hierarchy_dock
    dock.set_collapsed(True)
    main_window._restore_default_layout()
    qt_app.processEvents()
    assert not dock.collapsed and main_window.scene_hierarchy.isVisible()
    assert dock.toggle_button.arrowType() == Qt.LeftArrow


def test_collapsing_a_moved_hierarchy_brings_the_strip_back_to_the_left(main_window, qt_app):
    dock = main_window.scene_hierarchy_dock
    main_window.addDockWidget(Qt.RightDockWidgetArea, dock)
    dock.set_collapsed(True)
    qt_app.processEvents()
    assert main_window.dockWidgetArea(dock) == Qt.LeftDockWidgetArea
    assert dock.width() == dock.STRIP_WIDTH


def _settle(qt_app):
    # Window resizes re-apply the width from a zero-delay timer.
    for _ in range(3):
        qt_app.processEvents()


def test_the_hierarchy_starts_200px_wide(main_window, qt_app):
    dock = main_window.scene_hierarchy_dock
    main_window.resize(1600, 980)
    _settle(qt_app)
    assert dock.DEFAULT_WIDTH == 200
    assert dock.width() == 200


@pytest.mark.parametrize("final", [1280, 1920, 2560])
def test_the_window_reaching_its_size_late_keeps_the_hierarchy_width(main_window, qt_app, final):
    # Windows, X11 and Wayland maximise after the window is shown; macOS
    # before. The views take the change, never the hierarchy.
    dock = main_window.scene_hierarchy_dock
    main_window.resize(1600, 980)
    _settle(qt_app)
    main_window.resize(final, 980)
    _settle(qt_app)
    assert dock.width() == 200
    views = main_window.view_3d_dock.width() + main_window.right_dock.width()
    assert views > final - 200 - 120


def _drag_separator(window, dock, dx):
    """Drag the separator on the hierarchy's right edge by *dx* pixels."""
    from PyQt5.QtCore import QEvent, QPoint, QPointF
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QApplication
    x, y = dock.geometry().right() + 3, dock.geometry().center().y()
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, QPoint(x, y))
    for step in range(10, dx + 1, 10):
        # QTest.mouseMove carries no button state; a held-button move does.
        point = QPoint(x + step, y)
        QApplication.sendEvent(window, QMouseEvent(
            QEvent.MouseMove, QPointF(point), QPointF(window.mapToGlobal(point)),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        QApplication.processEvents()
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, QPoint(x + dx, y))


def test_a_dragged_width_survives_window_resizes_and_collapsing(main_window, qt_app):
    dock = main_window.scene_hierarchy_dock
    main_window.resize(1600, 980)
    _settle(qt_app)
    _drag_separator(main_window, dock, 60)
    _settle(qt_app)
    assert dock.width() == 260

    main_window.resize(1920, 980)
    _settle(qt_app)
    assert dock.width() == 260

    dock.set_collapsed(True)
    _settle(qt_app)
    assert dock.width() == dock.STRIP_WIDTH
    dock.set_collapsed(False)
    _settle(qt_app)
    assert dock.width() == 260
