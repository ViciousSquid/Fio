"""Tests for the default dock layout and how a saved one is restored.

The layout is written to settings.ini on every close, so ``restoreState()``
pins an install to the arrangement it first booted with.  These cover the
40/60 split the editor lays out, and the version gate that lets a changed
default actually reach an install that has been opened before.
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtCore import QByteArray, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from editor.ui import LAYOUT_VERSION  # noqa: E402

# Qt tier: PyQt5 must be importable.  No display and no GPU - the suite runs
# against the offscreen platform plugin.
pytestmark = pytest.mark.qt



# ────────────────────────────
# The 40/60 split
# ────────────────────────────

@pytest.mark.parametrize("width", [1280, 1600, 1920, 2560])
def test_the_3d_view_gets_two_fifths_of_the_width(main_window, qt_app, width):
    """The real MainWindow layout keeps the 3D/2D split at 40/60."""
    host = main_window
    host.resize(width, 980)
    host.show()
    qt_app.processEvents()

    shared = host.view_3d_dock.width() + host.right_dock.width()
    assert host.view_3d_dock.width() / shared == pytest.approx(0.40, abs=0.01)
    assert host.right_dock.width() / shared == pytest.approx(0.60, abs=0.01)


def test_the_2d_views_are_the_wider_of_the_two(main_window, qt_app):
    host = main_window
    qt_app.processEvents()
    assert host.right_dock.width() > host.view_3d_dock.width()


def test_the_layout_resize_is_applied_by_the_real_ui_owner(main_window, qt_app):
    host = main_window
    host.resizeDocks([host.view_3d_dock, host.right_dock], [40, 60], Qt.Horizontal)
    qt_app.processEvents()
    shared = host.view_3d_dock.width() + host.right_dock.width()
    assert host.view_3d_dock.width() / shared == pytest.approx(0.40, abs=0.01)
    assert host.right_dock.width() / shared == pytest.approx(0.60, abs=0.01)


def test_the_scene_hierarchy_keeps_its_zero_height_title_bar(main_window):
    dock = main_window.scene_hierarchy_dock
    assert dock.windowTitle() == "Scene Hierarchy"
    title_bar = dock.titleBarWidget()
    assert title_bar is not None
    assert title_bar.height() == 0

# ────────────────────────────
# The version gate
# ────────────────────────────

def _saved_layout(window, version=None):
    config = configparser.ConfigParser()
    config.add_section('Layout')
    config['Layout']['geometry'] = window.saveGeometry().toHex().data().decode()
    config['Layout']['state'] = window.saveState(LAYOUT_VERSION).toHex().data().decode()
    if version is not None:
        config['Layout']['version'] = str(version)
    return config


def _layout_toast(window, needle):
    return any(needle in label.text().lower()
               for label in window.findChildren(QLabel)
               if label.text())


def test_a_current_layout_is_restored(main_window, qt_app):
    host = main_window
    config = _saved_layout(host, LAYOUT_VERSION)
    host.addDockWidget(Qt.TopDockWidgetArea, host.view_3d_dock)
    assert host.toolBarArea(host.tool_toolbar) != Qt.RightToolBarArea
    host.config = config

    host.load_layout()

    assert host.dockWidgetArea(host.view_3d_dock) == Qt.RightDockWidgetArea


def test_a_layout_from_an_older_default_is_dropped(main_window, qt_app):
    config = _saved_layout(main_window, LAYOUT_VERSION - 1)
    main_window.config = config
    main_window.load_layout()
    assert not config.has_option('Layout', 'state')


def test_a_layout_saved_before_versioning_is_dropped(main_window):
    config = _saved_layout(main_window)
    config.remove_option('Layout', 'version')
    main_window.config = config
    main_window.load_layout()
    assert not config.has_option('Layout', 'state')


def test_dropping_it_says_so(main_window, qt_app):
    config = _saved_layout(main_window, LAYOUT_VERSION - 1)
    main_window.config = config
    main_window.load_layout()
    qt_app.processEvents()
    assert _layout_toast(main_window, 'layout reset')


def test_dropping_it_keeps_the_window_geometry(main_window):
    config = _saved_layout(main_window, LAYOUT_VERSION - 1)
    main_window.config = config
    main_window.load_layout()
    assert config.has_option('Layout', 'geometry')


def test_it_only_says_so_when_there_was_something_to_drop(main_window, qt_app):
    config = configparser.ConfigParser()
    config.add_section('Layout')
    config['Layout']['geometry'] = main_window.saveGeometry().toHex().data().decode()
    main_window.config = config
    main_window.load_layout()
    qt_app.processEvents()
    assert not _layout_toast(main_window, 'layout reset')


def test_no_layout_section_is_not_an_error(main_window):
    main_window.load_layout()
    assert not main_window.config.has_section('Layout')


def test_saving_stamps_the_version(main_window):
    main_window.save_layout()
    assert main_window.config.getint('Layout', 'version') == LAYOUT_VERSION


def test_a_layout_saved_now_is_restored_next_time(main_window, qt_app):
    host = main_window
    host.save_layout()
    host.addDockWidget(Qt.TopDockWidgetArea, host.view_3d_dock)
    host.load_layout()
    assert host.dockWidgetArea(host.view_3d_dock) == Qt.RightDockWidgetArea


def test_invalid_saved_state_falls_back_to_the_captured_default(main_window, qt_app):
    host = main_window
    config = _saved_layout(host, LAYOUT_VERSION)
    config['Layout']['state'] = QByteArray(b'invalid-layout-state').toHex().data().decode()
    host.config = config
    host.load_layout()
    qt_app.processEvents()
    assert not config.has_option('Layout', 'state')
    assert _layout_toast(host, 'invalid')


def test_reset_layout_restores_defaults_without_restarting(main_window, monkeypatch):
    from editor import main_window as mw

    host = main_window
    host.save_layout()
    host.addDockWidget(Qt.TopDockWidgetArea, host.view_3d_dock)

    monkeypatch.setattr(
        mw.QMessageBox, 'question',
        lambda *args, **kwargs: mw.QMessageBox.Yes)

    host.reset_layout()

    assert not host.config.has_section('Layout')
    assert host.dockWidgetArea(host.view_3d_dock) == Qt.RightDockWidgetArea


# ────────────────────────────
# What was really squeezing the 3D view
# ────────────────────────────

#: Parked so the widgets under test are not collected mid-assertion.  The
#: module's singletons rebuild themselves when their C++ side goes, so this
#: no longer has to paper over what ran before it.
_KEEP_ALIVE = []


def _debug_console(qt_app):
    """A DebugConsole built directly, around the module's two singletons."""
    import editor.debug_console as dc
    from editor.editor_state import EditorState

    class Host(QWidget):
        def __init__(self):
            super().__init__()
            self.state = EditorState()
            self.config = configparser.ConfigParser()

    host = Host()
    console = dc.DebugConsole(host)
    _KEEP_ALIVE.extend((host, console))
    return console


def test_the_debug_console_does_not_dictate_the_dock_width(qt_app):
    """This, not the ratio, is what pinned the 3D view to a slot.

    The console is tabbed into the dock column beneath the 2D views, so its
    minimum width is the whole column's.  Its toolbar is a dozen controls on
    one row whose minimums add up past 900px, which no resizeDocks ratio can
    argue with -- the 3D view got whatever was left, about 215px.
    """
    console = _debug_console(qt_app)

    assert console.minimumSizeHint().width() < 200


def test_the_console_toolbar_keeps_its_natural_size(qt_app):
    """It scrolls when there is no room; it does not squash or wrap."""
    console = _debug_console(qt_app)
    row = console._toolbar_scroll.widget()

    assert row.sizeHint().width() > 600
    assert console._toolbar_scroll.minimumWidth() < 200


def test_the_console_toolbar_scrolls_sideways_only(qt_app):
    from PyQt5.QtCore import Qt as _Qt

    console = _debug_console(qt_app)
    scroll = console._toolbar_scroll

    assert scroll.verticalScrollBarPolicy() == _Qt.ScrollBarAlwaysOff
    assert scroll.horizontalScrollBarPolicy() == _Qt.ScrollBarAsNeeded


def test_the_toolbar_is_not_covered_by_its_own_scroll_bar(qt_app):
    """The bar sits inside the scroll area, over the row it scrolls.

    Without room made for it, it hid most of the Filter row.
    """
    from PyQt5.QtWidgets import QMainWindow as _QMainWindow, QVBoxLayout

    console = _debug_console(qt_app)
    window = _QMainWindow()
    holder = QWidget()
    QVBoxLayout(holder).addWidget(console)
    window.setCentralWidget(holder)
    window.show()
    _KEEP_ALIVE.append(window)

    scroll = console._toolbar_scroll
    row_height = console._toolbar_row.sizeHint().height()

    window.resize(600, 500)                     # too narrow: the bar appears
    QApplication.instance().processEvents()
    QApplication.instance().processEvents()
    assert scroll.horizontalScrollBar().maximum() > 0
    assert scroll.height() > row_height

    window.resize(1400, 500)                    # room for it all: no bar
    QApplication.instance().processEvents()
    QApplication.instance().processEvents()
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.height() - row_height <= 4    # and so no gap either


# ────────────────────────────
# The console's singletons survive their widgets
# ────────────────────────────

def test_a_destroyed_logger_is_rebuilt(qt_app):
    """It is a singleton twice over, and used to hand back the corpse.

    Once the C++ object went, every later connect() raised and the Debug
    Console could never be built again for the life of the process.
    """
    from PyQt5 import sip

    import editor.debug_console as dc

    logger = dc.get_debug_logger()
    logger.deleteLater()
    sip.delete(logger)
    assert sip.isdeleted(logger)

    rebuilt = dc.get_debug_logger()

    assert not sip.isdeleted(rebuilt)
    assert rebuilt is not logger


def test_a_destroyed_console_is_rebuilt(qt_app):
    from PyQt5 import sip

    import editor.debug_console as dc
    from editor.editor_state import EditorState

    class Host(QWidget):
        def __init__(self):
            super().__init__()
            self.state = EditorState()
            self.config = configparser.ConfigParser()

    host = Host()
    _KEEP_ALIVE.append(host)
    console = dc.DebugConsole.get_instance(host)
    sip.delete(console)

    rebuilt = dc.DebugConsole.get_instance(host)

    assert not sip.isdeleted(rebuilt)
    _KEEP_ALIVE.append(rebuilt)


def test_a_live_logger_is_not_replaced(qt_app):
    """The recovery must not hand out a new logger on every call."""
    import editor.debug_console as dc

    assert dc.get_debug_logger() is dc.get_debug_logger()
