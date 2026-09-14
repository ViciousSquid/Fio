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

from editor.ui import Ui_MainWindow  # noqa: E402

#: The group colours the toolbar paints its strips with.
ORANGE = '#F08000'
BLUE = '#00A2E8'
GREY = '#555'


@pytest.fixture(scope="session")
def qt_app():
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
# The tool buttons are left as they were
# ────────────────────────────

def test_the_tool_buttons_keep_their_outline(toolbar):
    """Only the grid switch changed; the tools still show which is picked."""
    for button in (toolbar.select_tool_btn, toolbar.brush_tool_btn):
        checked = button.styleSheet().split('QPushButton:checked')[1]
        assert 'border: 1px solid %s;' % ORANGE in checked


def test_the_tool_buttons_keep_their_group_strip(toolbar):
    for button in (toolbar.select_tool_btn, toolbar.brush_tool_btn):
        assert 'border-bottom: 3px solid %s;' % ORANGE in button.styleSheet()


def test_only_the_grid_button_uses_the_toggle_strip(toolbar):
    """A grey strip anywhere else would mean the change leaked."""
    greyed = [b for b in toolbar.tool_toolbar.findChildren(QPushButton)
              if 'border-bottom: 3px solid %s;' % GREY in b.styleSheet()]

    assert greyed == [toolbar.grid_btn]
