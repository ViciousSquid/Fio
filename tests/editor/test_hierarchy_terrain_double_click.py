"""Double-clicking Terrain in the scene hierarchy opens the terrain editor."""

import pytest

pytest.importorskip("PyQt5", reason="the scene hierarchy is a Qt widget")

pytestmark = pytest.mark.qt


def _terrain_item(hierarchy):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QTreeWidgetItemIterator
    it = QTreeWidgetItemIterator(hierarchy.tree)
    while it.value():
        data = it.value().data(0, Qt.UserRole)
        if data and data[0] == 'terrain':
            return it.value()
        it += 1
    return None


def _send(widget, etype, pos):
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QMouseEvent
    from PyQt5.QtWidgets import QApplication
    buttons = Qt.NoButton if etype == QMouseEvent.MouseButtonRelease else Qt.LeftButton
    QApplication.sendEvent(widget, QMouseEvent(etype, pos, Qt.LeftButton, buttons, Qt.NoModifier))


def _double_click(qt_app, tree, item):
    from PyQt5.QtCore import QEvent
    tree.scrollToItem(item)
    pos = tree.visualItemRect(item).center()
    viewport = tree.viewport()
    # What a real double-click delivers: press, release, double-click, release.
    _send(viewport, QEvent.MouseButtonPress, pos)
    _send(viewport, QEvent.MouseButtonRelease, pos)
    qt_app.processEvents()
    _send(viewport, QEvent.MouseButtonDblClick, pos)
    _send(viewport, QEvent.MouseButtonRelease, pos)
    qt_app.processEvents()


@pytest.mark.parametrize("brush_selected_first", [False, True])
def test_double_clicking_terrain_opens_the_terrain_editor(
        main_window, qt_app, monkeypatch, brush_selected_first):
    from engine.terrain import Terrain
    from tests.helpers.worlds import box_brush
    main_window.terrain = Terrain(seed=1)
    main_window.state.terrain_data = main_window.terrain.to_dict()
    brush = box_brush("wall")
    main_window.state.brushes.append(brush)
    if brush_selected_first:
        # The first click then has to clear a selection, rebuilding the tree.
        main_window.set_selected_objects([brush])
    hierarchy = main_window.scene_hierarchy
    hierarchy.refresh_list()
    qt_app.processEvents()

    opened = []
    monkeypatch.setattr(main_window, "open_terrain_editor", lambda: opened.append(True))
    item = _terrain_item(hierarchy)
    assert item is not None
    _double_click(qt_app, hierarchy.tree, item)
    assert opened == [True]


def test_double_clicking_a_brush_does_not_open_the_terrain_editor(
        main_window, qt_app, monkeypatch):
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QTreeWidgetItemIterator
    from engine.terrain import Terrain
    from tests.helpers.worlds import box_brush
    main_window.terrain = Terrain(seed=1)
    main_window.state.terrain_data = main_window.terrain.to_dict()
    main_window.state.brushes.append(box_brush("wall"))
    hierarchy = main_window.scene_hierarchy
    hierarchy.refresh_list()
    qt_app.processEvents()
    opened = []
    monkeypatch.setattr(main_window, "open_terrain_editor", lambda: opened.append(True))
    it = QTreeWidgetItemIterator(hierarchy.tree)
    while it.value() and (it.value().data(0, Qt.UserRole) or ("",))[0] != "brush":
        it += 1
    assert it.value() is not None
    _double_click(qt_app, hierarchy.tree, it.value())
    assert opened == []
