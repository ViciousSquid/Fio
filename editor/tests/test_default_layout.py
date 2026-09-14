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
from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QDockWidget, QMainWindow, QWidget,
)

from editor.ui import LAYOUT_VERSION  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


# ────────────────────────────
# The 40/60 split
# ────────────────────────────

def _lay_out(width=1600, height=980):
    """The editor's dock arrangement, with stand-ins for the real views."""
    window = QMainWindow()
    window.resize(width, height)

    def dock(title, name, minimum=None):
        widget = QDockWidget(title, window)
        widget.setObjectName(name)
        widget.setWidget(QWidget())
        if minimum:
            widget.setMinimumWidth(minimum)
        return widget

    scene = dock("Scene", "SceneDock")
    window.addDockWidget(Qt.LeftDockWidgetArea, scene)
    scene.setMaximumWidth(int(width * 0.10))

    view_3d = dock("3D View", "View3DDock")
    window.addDockWidget(Qt.RightDockWidgetArea, view_3d)
    views_2d = dock("2D Views", "2DViewsDock", 610)
    window.addDockWidget(Qt.RightDockWidgetArea, views_2d)
    properties = dock(" ", "PropertiesDock")
    window.addDockWidget(Qt.RightDockWidgetArea, properties)

    window.splitDockWidget(view_3d, views_2d, Qt.Horizontal)
    window.splitDockWidget(views_2d, properties, Qt.Vertical)
    window.resizeDocks([view_3d, views_2d], [40, 60], Qt.Horizontal)
    window.resizeDocks([views_2d, properties], [600, 300], Qt.Vertical)

    assets = dock("Asset Browser", "AssetBrowserDock")
    window.addDockWidget(Qt.RightDockWidgetArea, assets)
    window.splitDockWidget(view_3d, assets, Qt.Vertical)
    window.resizeDocks([view_3d, assets], [10000, 1], Qt.Vertical)

    window.show()
    QApplication.instance().processEvents()
    return window, view_3d, views_2d


@pytest.mark.parametrize("width", [1280, 1600, 1920, 2560])
def test_the_3d_view_gets_two_fifths_of_the_width(qt_app, width):
    """The ratio is what matters, so it has to hold at any window size."""
    _, view_3d, views_2d = _lay_out(width)
    shared = view_3d.width() + views_2d.width()

    assert view_3d.width() / shared == pytest.approx(0.40, abs=0.01)
    assert views_2d.width() / shared == pytest.approx(0.60, abs=0.01)


def test_the_2d_views_are_the_wider_of_the_two(qt_app):
    _, view_3d, views_2d = _lay_out()

    assert views_2d.width() > view_3d.width()


def test_the_layout_call_asks_for_forty_sixty(qt_app):
    """The ratio lives in ui.py; this is what the numbers above come from."""
    import inspect

    from editor.ui import Ui_MainWindow

    source = inspect.getsource(Ui_MainWindow.setupUi)

    assert '[40, 60], Qt.Horizontal' in source


# ────────────────────────────
# The version gate
# ────────────────────────────

class FakeEditorWindow(QMainWindow):
    """Enough of MainWindow for the two layout methods, which are bound on."""

    from editor.main_window import MainWindow
    load_layout = MainWindow.load_layout
    save_layout = MainWindow.save_layout
    del MainWindow

    def __init__(self, config=None):
        super().__init__()
        self.config = config if config is not None else configparser.ConfigParser()
        self.toasts = []
        self.saved_config = 0
        self.restored = []

    def show_toast(self, message, is_error=False, duration=None):
        self.toasts.append(message)

    def save_config(self):
        self.saved_config += 1

    def restoreState(self, data):
        self.restored.append(bytes(data))
        return True


def _saved_layout(version=None):
    config = configparser.ConfigParser()
    config.add_section('Layout')
    config['Layout']['geometry'] = QByteArray(b'geom').toHex().data().decode()
    config['Layout']['state'] = QByteArray(b'state').toHex().data().decode()
    if version is not None:
        config['Layout']['version'] = str(version)
    return config


def test_a_current_layout_is_restored(qt_app):
    host = FakeEditorWindow(_saved_layout(LAYOUT_VERSION))

    host.load_layout()

    assert host.restored == [b'state']
    assert host.toasts == []


def test_a_layout_from_an_older_default_is_dropped(qt_app):
    """Otherwise a changed default never reaches an install that has run."""
    config = _saved_layout(LAYOUT_VERSION - 1)
    host = FakeEditorWindow(config)

    host.load_layout()

    assert host.restored == []
    assert not config.has_option('Layout', 'state')


def test_a_layout_saved_before_versioning_is_dropped(qt_app):
    """No version key at all means it predates the gate."""
    config = _saved_layout()
    assert not config.has_option('Layout', 'version')
    host = FakeEditorWindow(config)

    host.load_layout()

    assert host.restored == []


def test_dropping_it_says_so(qt_app):
    host = FakeEditorWindow(_saved_layout(LAYOUT_VERSION - 1))

    host.load_layout()
    QApplication.instance().processEvents()      # the toast is deferred

    assert any('layout' in t.lower() for t in host.toasts)


def test_dropping_it_keeps_the_window_geometry(qt_app):
    """Where the window sits is the user's doing, not the default's."""
    config = _saved_layout(LAYOUT_VERSION - 1)
    host = FakeEditorWindow(config)

    host.load_layout()

    assert config.has_option('Layout', 'geometry')


def test_it_only_says_so_when_there_was_something_to_drop(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Layout')
    config['Layout']['geometry'] = QByteArray(b'geom').toHex().data().decode()
    host = FakeEditorWindow(config)

    host.load_layout()
    QApplication.instance().processEvents()

    assert host.toasts == []


def test_no_layout_section_is_not_an_error(qt_app):
    host = FakeEditorWindow()

    host.load_layout()

    assert host.restored == []


def test_saving_stamps_the_version(qt_app):
    host = FakeEditorWindow()

    host.save_layout()

    assert host.config.getint('Layout', 'version') == LAYOUT_VERSION


def test_a_layout_saved_now_is_restored_next_time(qt_app):
    """The round trip the gate must not break."""
    host = FakeEditorWindow()
    host.save_layout()

    reopened = FakeEditorWindow(host.config)
    reopened.load_layout()

    assert reopened.restored
    assert reopened.toasts == []
