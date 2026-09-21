"""Scene Hierarchy → right click → Properties.

Opening the panel is three things rather than one, because the Properties dock
is tabbed with the Debug Console and can be closed outright: the dock visible,
the dock raised, and the Properties tab selected. And the object has to be
selected explicitly — ``open_menu`` selects the item under the cursor with
signals blocked, so ``handle_selection_change`` never runs and the panel would
otherwise still be showing whatever was selected before the right-click.
"""

import re
import types

import pytest

pytest.importorskip("PyQt5", reason="the hierarchy panel is editor-tier")

from PyQt5.QtWidgets import QDockWidget, QTabWidget, QWidget   # noqa: E402

from editor.main_window import MainWindow                      # noqa: E402
from editor.scene_hierarchy import SceneHierarchy              # noqa: E402
from engine.prop_entity import Prop                            # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def hierarchy(qt_app):
    """A hierarchy wired to the real dock/tab shape the editor builds."""
    prop_editor, console = QWidget(), QWidget()
    tabs = QTabWidget()
    tabs.addTab(prop_editor, "Properties")
    tabs.addTab(console, "Debug Console")
    dock = QDockWidget()
    dock.setWidget(tabs)
    dock.setVisible(False)
    tabs.setCurrentIndex(tabs.indexOf(console))    # start on the wrong tab

    thing = Prop(pos=[0, 0, 0])
    brush = {"name": "wall", "pos": [0, 0, 0], "size": [64, 64, 64]}
    selected = []

    window = types.SimpleNamespace(
        state=types.SimpleNamespace(things=[thing], brushes=[brush]),
        property_editor=prop_editor,
        properties_tab_widget=tabs,
        properties_dock=dock,
        debug_console=console,
        set_selected_objects=lambda objs: selected.append(list(objs)),
    )
    window.show_properties_panel = types.MethodType(
        MainWindow.show_properties_panel, window)

    panel = SceneHierarchy.__new__(SceneHierarchy)
    panel.main_window = window
    yield panel, window, selected, thing, brush
    dock.deleteLater()


def current_tab(window):
    tabs = window.properties_tab_widget
    return tabs.tabText(tabs.currentIndex())


def test_it_switches_to_the_properties_tab(hierarchy):
    panel, window, _selected, thing, _brush = hierarchy
    assert current_tab(window) == "Debug Console"

    panel.show_properties_for(thing)

    assert current_tab(window) == "Properties"


def test_it_reveals_a_closed_properties_dock(hierarchy):
    panel, window, _selected, thing, _brush = hierarchy
    assert not window.properties_dock.isVisible()

    panel.show_properties_for(thing)

    assert window.properties_dock.isVisible(), (
        "Properties was chosen but the dock stayed closed")


def test_it_selects_the_item_the_menu_was_opened_on(hierarchy):
    """The panel shows a selection, so the selection has to be made."""
    panel, _window, selected, thing, _brush = hierarchy

    panel.show_properties_for(thing)

    assert selected and selected[-1] == [thing], (
        "the panel would show whatever was selected before the right-click")


def test_it_works_for_a_brush_too(hierarchy):
    panel, window, selected, _thing, brush = hierarchy

    panel.show_properties_for(brush)

    assert selected[-1] == [brush]
    assert current_tab(window) == "Properties"


def test_nothing_happens_without_an_object(hierarchy):
    panel, window, selected, _thing, _brush = hierarchy

    panel.show_properties_for(None)

    assert selected == []
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
