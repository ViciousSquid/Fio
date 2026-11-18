"""Ctrl-clicking rows of the Scene Hierarchy adds them to the selection
without the list jumping: every selection change rebuilds the tree, and the
rebuild used to scroll to the first selected row -- the top of a multi-row
selection -- on each click."""

import pytest

pytest.importorskip('PyQt5', reason='the hierarchy is a Qt widget')

from PyQt5.QtCore import Qt                                     # noqa: E402
from PyQt5.QtTest import QTest                                   # noqa: E402

from editor.things import Light                                  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def hierarchy(main_window, qt_app):
    logic = main_window.view_3d.logic_thread
    if logic is not None:
        logic.stop()
        if logic.is_alive():
            logic.join(timeout=1)
    main_window.state.things.extend(
        Light(pos=[float(i * 40), 0.0, 0.0], properties={"name": "lamp_%03d" % i})
        for i in range(120))
    panel = main_window.scene_hierarchy
    panel.refresh_list()
    panel.tree.resize(260, 300)
    panel.show()
    qt_app.processEvents()
    return panel


def _row(panel, name):
    tree = panel.tree
    for i in range(tree.topLevelItemCount()):
        header = tree.topLevelItem(i)
        for j in range(header.childCount()):
            item = header.child(j)
            if name in item.text(0):
                return item
    raise AssertionError(name)


def _click(panel, name, qt_app, modifier=Qt.NoModifier):
    item = _row(panel, name)
    rect = panel.tree.visualItemRect(item)
    assert rect.isValid() and panel.tree.viewport().rect().contains(rect.center()), name
    viewport = panel.tree.viewport()
    if modifier == Qt.ControlModifier:
        QTest.keyPress(viewport, Qt.Key_Control)          # held, as a keyboard does
    QTest.mouseClick(viewport, Qt.LeftButton, modifier, rect.center())
    if modifier == Qt.ControlModifier:
        QTest.keyRelease(viewport, Qt.Key_Control)
    qt_app.processEvents()


def test_ctrl_clicking_rows_keeps_the_list_still(hierarchy, qt_app):
    bar = hierarchy.tree.verticalScrollBar()
    hierarchy.tree.scrollToItem(_row(hierarchy, "lamp_060"))
    qt_app.processEvents()
    top = bar.value()
    assert top > 0
    visible = [item.text(0) for item in (
        hierarchy.tree.itemAt(0, y) for y in range(5, 280, 18)) if item is not None]
    first, later = visible[1], visible[-3]
    _click(hierarchy, first, qt_app)
    assert bar.value() == top
    _click(hierarchy, later, qt_app, Qt.ControlModifier)
    assert bar.value() == top                                   # no jump
    selected = {o.properties["name"] for o in hierarchy.main_window.state.selected_objects}
    assert len(selected) == 2 and all(any(n in row for n in selected) for row in (first, later))
    # The row clicked last stays current: a Shift-click ranges from it.
    assert later in hierarchy.tree.currentItem().text(0)


def test_a_selection_from_elsewhere_brings_its_row_into_view(hierarchy, qt_app):
    bar = hierarchy.tree.verticalScrollBar()
    bar.setValue(0)
    lamp = next(t for t in hierarchy.main_window.state.things
                if t.properties.get("name") == "lamp_110")
    hierarchy.main_window.set_selected_objects([lamp])          # e.g. picked in 2D
    qt_app.processEvents()
    item = _row(hierarchy, "lamp_110")
    assert hierarchy.tree.viewport().rect().intersects(hierarchy.tree.visualItemRect(item))
