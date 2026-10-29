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
