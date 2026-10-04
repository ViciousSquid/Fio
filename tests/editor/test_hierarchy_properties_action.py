"""Scene Hierarchy → right click → Properties.

Opening the panel is three things rather than one, because the Properties dock
is tabbed with the Debug Console and can be closed outright: the dock visible,
the dock raised, and the Properties tab selected. And the object has to be
selected explicitly — ``open_menu`` selects the item under the cursor with
signals blocked, so ``handle_selection_change`` never runs and the panel would
otherwise still be showing whatever was selected before the right-click.
"""

import re

import pytest

pytest.importorskip("PyQt5", reason="the hierarchy panel is editor-tier")

from PyQt5.QtWidgets import QDockWidget, QTabWidget, QWidget   # noqa: E402

from editor.main_window import MainWindow                      # noqa: E402
from editor.scene_hierarchy import SceneHierarchy              # noqa: E402
from engine.prop_entity import Prop                            # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def hierarchy(main_window):
    window = main_window
    thing = Prop(pos=[0, 0, 0])
    brush = {"name": "wall", "pos": [0, 0, 0], "size": [64, 64, 64]}
    window.state.things[:] = [thing]
    window.state.brushes[:] = [brush]
    window.set_selected_objects([])
    window.properties_dock.setVisible(False)
    debug_index = window.properties_tab_widget.indexOf(window.debug_console)
    if debug_index >= 0:
        window.properties_tab_widget.setCurrentIndex(debug_index)
    yield window.scene_hierarchy, window, thing, brush



def current_tab(window):
    tabs = window.properties_tab_widget
    return tabs.tabText(tabs.currentIndex())


def test_it_switches_to_the_properties_tab(hierarchy):
    panel, window, thing, _brush = hierarchy
    assert current_tab(window) == "Debug Console"

    panel.show_properties_for(thing)

    assert current_tab(window) == "Properties"


def test_it_reveals_a_closed_properties_dock(hierarchy):
    panel, window, thing, _brush = hierarchy
    assert not window.properties_dock.isVisible()

    panel.show_properties_for(thing)

    assert window.properties_dock.isVisible(), (
        "Properties was chosen but the dock stayed closed")


def test_it_selects_the_item_the_menu_was_opened_on(hierarchy):
    """The panel shows a selection, so the selection has to be made."""
    panel, window, thing, _brush = hierarchy

    panel.show_properties_for(thing)

    assert window.state.selected_objects == [thing], (
        "the panel would show whatever was selected before the right-click")


def test_it_works_for_a_brush_too(hierarchy):
    panel, window, _thing, brush = hierarchy

    panel.show_properties_for(brush)

    assert window.state.selected_objects == [brush]
    assert current_tab(window) == "Properties"


def test_nothing_happens_without_an_object(hierarchy):
    panel, window, _thing, _brush = hierarchy

    panel.show_properties_for(None)

    assert window.state.selected_objects == []
    assert not window.properties_dock.isVisible()


# ---------------------------------------------------------------------------
# Placement in the menu
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("branch", ["brush", "thing"])
def test_properties_is_the_last_entry_in_the_menu(branch):
    """"At the bottom" is the requirement, so it is what gets asserted.

    Read as source: the menu is built and consumed inside a blocking
    ``menu.exec_()``, which a test cannot step through.
    """
    source = open("editor/scene_hierarchy.py", encoding="utf-8").read()
    start = source.index("if data and data[0] == '%s':" % branch)
    end = source.index("menu.exec_", start)
    body = source[start:end]

    entries = re.findall(r"menu\.add(?:Action|Menu)\(\s*\"([^\"]+)\"", body)
    assert entries, "no menu entries found in the %s branch" % branch
    assert entries[-1] == "Properties", (
        "Properties is not the last entry in the %s menu; order is %r"
        % (branch, entries))
