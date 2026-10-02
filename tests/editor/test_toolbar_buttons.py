"""Tests for the tool toolbar's button styling.

The real ``create_tool_toolbar`` is run against a stand-in window, so these
check the buttons the editor actually builds rather than a copy of the
stylesheet kept in the test.
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import (  # noqa: E402
    QAction, QApplication, QMainWindow, QPushButton,
)

from editor.main_window import MainWindow  # noqa: E402
from editor.ui import Ui_MainWindow  # noqa: E402

# Qt tier: PyQt5 must be importable.  No display and no GPU - the suite runs
# against the offscreen platform plugin.
pytestmark = pytest.mark.qt


#: The group colours the toolbar paints its strips with.
ORANGE = '#F08000'
BLUE = '#00A2E8'
GREY = '#555'


@pytest.fixture(scope="session")
def qt_app():
    # Only when there is no display: the offscreen plugin cannot create an
    # OpenGL context, and forcing it here would disable the visual tier for
    # the whole session when the suite is run under Xvfb.
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class FakeEditorWindow(QMainWindow):
    """The slice of MainWindow ``create_tool_toolbar`` reaches for.

    Every callback it wires up is a no-op here: the toolbar is being built
    to look at, not to drive.
    """

    tool_mode = 'select'

    def __init__(self):
        super().__init__()
        self.config = configparser.ConfigParser()
        self.play_button = QPushButton("Play")
        self.terrain_action = QAction("Terrain", self)
        self.procedural_action = QAction("Procedural", self)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def toolbar(qt_app):
    window = FakeEditorWindow()
    Ui_MainWindow().create_tool_toolbar(window)
    return window


# ────────────────────────────
# The grid toggle
# ────────────────────────────

def test_the_grid_button_has_no_orange_outline(toolbar):
    """It is a switch, not one of the tools, so it should not read as picked."""
    assert ORANGE not in toolbar.grid_btn.styleSheet()


def test_the_grid_button_lights_its_strip_when_on(toolbar):
    sheet = toolbar.grid_btn.styleSheet()
    checked = sheet.split('QPushButton:checked')[1]

    assert 'border-bottom: 3px solid %s;' % BLUE in checked


def test_the_grid_button_greys_its_strip_when_off(toolbar):
    sheet = toolbar.grid_btn.styleSheet()
    unchecked = sheet.split('QPushButton:checked')[0]

    assert 'border-bottom: 3px solid %s;' % GREY in unchecked


def test_the_grid_button_matches_its_neighbour_when_on(toolbar):
    """Blue underneath, the same as the Procedural Tools button beside it."""
    buttons = toolbar.tool_toolbar.findChildren(QPushButton)
    grid_index = buttons.index(toolbar.grid_btn)
    neighbour = buttons[grid_index - 1]

    lit = toolbar.grid_btn.styleSheet().split('QPushButton:checked')[1]
    assert 'border-bottom: 3px solid %s;' % BLUE in lit
    assert 'border-bottom: 3px solid %s;' % BLUE in neighbour.styleSheet()


def test_the_grid_button_starts_on(toolbar):
    assert toolbar.grid_btn.isChecked()


# ────────────────────────────
# The base-tool group: Select / Brush / Vertex / Edge / Face
# ────────────────────────────

#: The five buttons that are one group on the toolbar — picking any of them
#: changes what a drag in a 2D view does.
TOOL_GROUP = ('select_tool_btn', 'brush_tool_btn',
              'vertex_mode_btn', 'edge_mode_btn', 'face_mode_btn')


def _group_buttons(toolbar):
    return [getattr(toolbar, name) for name in TOOL_GROUP]


@pytest.mark.parametrize("name", TOOL_GROUP)
def test_a_tool_button_greys_its_strip_when_it_is_not_the_active_tool(toolbar, name):
    """The strip is the state, exactly as on the grid switch."""
    sheet = getattr(toolbar, name).styleSheet()
    unchecked = sheet.split('QPushButton:checked')[0]

    assert 'border-bottom: 3px solid %s;' % GREY in unchecked, (
        "%s paints its strip before it is checked, so more than one tool "
        "reads as picked" % name)


@pytest.mark.parametrize("name", TOOL_GROUP)
def test_a_tool_button_lights_its_strip_when_it_is_the_active_tool(toolbar, name):
    sheet = getattr(toolbar, name).styleSheet()
    checked = sheet.split('QPushButton:checked')[1]

    assert 'border-bottom: 3px solid %s;' % ORANGE in checked, (
        "%s does not light up when picked" % name)


def test_the_tool_buttons_show_state_the_same_way_the_grid_button_does(toolbar):
    """The request was literally "copy the grid button", so check that."""
    grid = toolbar.grid_btn.styleSheet().replace(BLUE, '<colour>')

    for name in TOOL_GROUP:
        sheet = getattr(toolbar, name).styleSheet().replace(ORANGE, '<colour>')
        assert sheet == grid, (
            "%s is styled differently from the grid switch it is meant to "
            "copy" % name)


def test_only_one_tool_button_is_checked_at_a_time(qt_app):
    """Whatever the editor's state, exactly one strip in the group is lit.

    Driven through the real ``_sync_tool_group_buttons`` — the method the
    editor calls after every tool and component-mode change — against the
    buttons ``create_tool_toolbar`` actually builds.
    """
    from editor import component_edit as ce

    class SyncingWindow(FakeEditorWindow):
        _sync_tool_group_buttons = MainWindow._sync_tool_group_buttons

        def __init__(self):
            super().__init__()
            self.components = ce.ComponentController()
            self.tool_mode = 'select'

    window = SyncingWindow()
    Ui_MainWindow().create_tool_toolbar(window)

    cases = [
        ('select', ce.MODE_OBJECT, 'select_tool_btn'),
        ('brush', ce.MODE_OBJECT, 'brush_tool_btn'),
        ('select', ce.MODE_VERTEX, 'vertex_mode_btn'),
        ('select', ce.MODE_EDGE, 'edge_mode_btn'),
        ('select', ce.MODE_FACE, 'face_mode_btn'),
        # A component mode supersedes the base tool, whichever it is.
        ('brush', ce.MODE_FACE, 'face_mode_btn'),
    ]
    for tool_mode, component_mode, expected in cases:
        window.tool_mode = tool_mode
        window.components.mode = component_mode
        window._sync_tool_group_buttons()

        lit = [name for name in TOOL_GROUP if getattr(window, name).isChecked()]
        assert lit == [expected], (
            "tool_mode=%r, component mode=%r: expected only %s to be lit, "
            "got %s" % (tool_mode, component_mode, expected, lit or "nothing"))


def test_the_editing_action_buttons_keep_their_plain_group_strip(toolbar):
    """The change is scoped to the base-tool group; the green strip is a
    grouping colour on buttons that are not tools, and stays on."""
    green_strip = 'border-bottom: 3px solid #22b14c;'
    buttons = toolbar.tool_toolbar.findChildren(QPushButton)
    green = [b for b in buttons if green_strip in b.styleSheet()]

    assert green, "the editing-action group lost its colour entirely"
    for button in green:
        unchecked = button.styleSheet().split('QPushButton:checked')[0]
        assert green_strip in unchecked


def test_the_greyed_strip_is_used_by_the_toggles_and_nothing_else(toolbar):
    """A grey strip means "checkable, and not currently on"."""
    greyed = {b for b in toolbar.tool_toolbar.findChildren(QPushButton)
              if 'border-bottom: 3px solid %s;' % GREY in b.styleSheet()}

    assert greyed == set(_group_buttons(toolbar)) | {toolbar.grid_btn}
