"""Tests for Settings > Editor > Tooltips.

Two switches, one per area, that take the tooltips off the Property Editor
and off the toolbar.  Covered here: the stash-and-restore helper they are
built on, that the dialog reads and writes the settings, and that the
Property Editor keeps the setting across the page rebuilds and cached pages
that its selection handling is full of.
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QGroupBox, QPushButton, QVBoxLayout, QWidget,
)

from editor.editor_state import EditorState  # noqa: E402
from editor.property_editor import PropertyEditor  # noqa: E402
from editor.tooltips import STASH, set_tooltips_enabled  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


# ────────────────────────────
# The stash-and-restore helper
# ────────────────────────────

@pytest.fixture
def tree(qt_app):
    """A parent with a tooltip and two children, one without one."""
    root = QWidget()
    layout = QVBoxLayout(root)
    root.setToolTip("root tip")
    tipped = QPushButton()
    tipped.setToolTip("child tip")
    bare = QPushButton()
    layout.addWidget(tipped)
    layout.addWidget(bare)
    return root, tipped, bare


def test_hiding_takes_every_tooltip_including_the_root_s(tree):
    root, tipped, bare = tree

    set_tooltips_enabled(root, False)

    assert root.toolTip() == ''
    assert tipped.toolTip() == ''


def test_showing_puts_them_all_back(tree):
    root, tipped, bare = tree

    set_tooltips_enabled(root, False)
    set_tooltips_enabled(root, True)

    assert root.toolTip() == 'root tip'
    assert tipped.toolTip() == 'child tip'


def test_hiding_twice_does_not_lose_the_text(tree):
    """The second pass sees a blank tooltip and must not stash that."""
    root, tipped, bare = tree

    set_tooltips_enabled(root, False)
    set_tooltips_enabled(root, False)
    set_tooltips_enabled(root, True)

    assert tipped.toolTip() == 'child tip'


def test_showing_a_widget_that_was_never_hidden_leaves_it_alone(tree):
    root, tipped, bare = tree

    set_tooltips_enabled(root, True)

    assert tipped.toolTip() == 'child tip'
    assert bare.toolTip() == ''


def test_a_widget_with_no_tooltip_is_not_given_one(tree):
    root, tipped, bare = tree

    set_tooltips_enabled(root, False)
    set_tooltips_enabled(root, True)

    assert bare.toolTip() == ''
    assert not bare.property(STASH)


def test_the_stash_is_cleared_once_restored(tree):
    root, tipped, bare = tree

    set_tooltips_enabled(root, False)
    assert tipped.property(STASH) == 'child tip'
    set_tooltips_enabled(root, True)

    assert not tipped.property(STASH)


def test_no_root_is_not_an_error():
    set_tooltips_enabled(None, False)


# ────────────────────────────
# The Settings dialog
# ────────────────────────────

def _settings(config):
    from editor.SettingsWindow import SettingsWindow
    return SettingsWindow(config)


def _editor_tab(dialog):
    for i in range(dialog.tabs.count()):
        if dialog.tabs.tabText(i) == 'Editor':
            return dialog.tabs.widget(i)
    return None


def test_the_editor_tab_has_a_tooltips_section(qt_app):
    dialog = _settings(configparser.ConfigParser())
    tab = _editor_tab(dialog)

    assert tab is not None
    titles = [g.title() for g in tab.findChildren(QGroupBox)]
    assert 'Tooltips' in titles


def test_the_section_holds_the_two_switches(qt_app):
    dialog = _settings(configparser.ConfigParser())
    tab = _editor_tab(dialog)
    group = next(g for g in tab.findChildren(QGroupBox) if g.title() == 'Tooltips')

    labels = [c.text() for c in group.findChildren(QCheckBox)]

    assert labels == ['Property Editor', 'Toolbar']


def test_both_default_to_on(qt_app):
    dialog = _settings(configparser.ConfigParser())

    assert dialog.property_editor_tooltips_checkbox.isChecked()
    assert dialog.toolbar_tooltips_checkbox.isChecked()


def test_the_settings_are_read_back(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Editor')
    config.set('Editor', 'property_editor_tooltips', 'False')
    config.set('Editor', 'toolbar_tooltips', 'True')

    dialog = _settings(config)

    assert not dialog.property_editor_tooltips_checkbox.isChecked()
    assert dialog.toolbar_tooltips_checkbox.isChecked()


def test_the_settings_are_written_out(qt_app):
    config = configparser.ConfigParser()
    dialog = _settings(config)

    dialog.property_editor_tooltips_checkbox.setChecked(False)
    dialog.toolbar_tooltips_checkbox.setChecked(False)
    dialog._save_settings()

    assert not config.getboolean('Editor', 'property_editor_tooltips')
    assert not config.getboolean('Editor', 'toolbar_tooltips')


def test_saving_does_not_disturb_the_autosave_settings(qt_app):
    """Editor is an existing section; writing into it must not clear it."""
    config = configparser.ConfigParser()
    config.add_section('Editor')
    config.set('Editor', 'autosave_enabled', 'False')
    config.set('Editor', 'autosave_interval', '25')

    dialog = _settings(config)
    dialog._save_settings()

    assert config.get('Editor', 'autosave_enabled') == 'False'
    assert config.get('Editor', 'autosave_interval') == '25'


# ────────────────────────────
# The Property Editor honouring it
# ────────────────────────────

class FakeHost(QWidget):
    def __init__(self):
        super().__init__()
        self.state = EditorState()
        self.state.selected_objects = []
        self.config = configparser.ConfigParser()
        self.grid_size = 16

    def save_state(self):
        pass

    def update_views(self):
        pass

    def update_all_ui(self):
        pass

    def show_toast(self, message, is_error=False, duration=None):
        pass


def make_brush(name='wall'):
    return {
        'pos': [0, 0, 0],
        'size': [128, 64, 32],
        'name': name,
        'id': 'id-%s' % name,
        'textures': {tag: 'default.png' for tag in bg.FACE_TAGS},
    }


@pytest.fixture
def panel(qt_app):
    host = FakeHost()
    return host, PropertyEditor(host)


def _tooltips(widget):
    return [w.toolTip() for w in widget.findChildren(QWidget) if w.toolTip()]


def test_the_panel_has_tooltips_to_begin_with(panel):
    """Guards every test below: they would pass vacuously on a bare panel."""
    host, editor = panel
    editor.set_object(make_brush())

    assert _tooltips(editor)


def test_switching_them_off_clears_the_page_on_screen(panel):
    host, editor = panel
    editor.set_object(make_brush())

    editor.set_tooltips_enabled(False)

    assert _tooltips(editor) == []


def test_switching_them_back_on_restores_the_page(panel):
    host, editor = panel
    editor.set_object(make_brush())
    before = sorted(_tooltips(editor))

    editor.set_tooltips_enabled(False)
    editor.set_tooltips_enabled(True)

    assert sorted(_tooltips(editor)) == before


def test_a_page_built_later_comes_up_without_tooltips(panel):
    """set_object() rebuilds constantly; each new page must honour it."""
    host, editor = panel
    editor.set_tooltips_enabled(False)

    editor.set_object(make_brush('one'))

    assert _tooltips(editor) == []


def test_a_page_restored_from_the_cache_stays_stripped(panel):
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]
    editor.set_tooltips_enabled(False)

    editor.set_object(one)
    editor.set_object(two)
    editor.set_object(one)          # comes back from the page cache

    assert _tooltips(editor) == []


def test_switching_on_reaches_pages_parked_in_the_cache(panel):
    """Otherwise the setting reappears to change back on an old selection."""
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]

    editor.set_object(one)
    editor.set_tooltips_enabled(False)
    editor.set_object(two)          # one is parked, stripped
    editor.set_tooltips_enabled(True)
    editor.set_object(one)          # restored from the cache

    assert _tooltips(editor)


def test_the_setting_survives_a_page_restore(panel):
    """_restore_page_state() wipes attributes; this one is the editor's."""
    host, editor = panel
    one, two = make_brush('one'), make_brush('two')
    host.state.brushes = [one, two]
    editor.set_tooltips_enabled(False)

    editor.set_object(one)
    editor.set_object(two)
    editor.set_object(one)

    assert editor._tooltips_enabled is False


def test_leaving_them_on_costs_no_walk(panel, monkeypatch):
    """The common case must not walk the widget tree on every selection."""
    import editor.property_editor as pe

    host, editor = panel
    calls = []
    monkeypatch.setattr(pe, 'set_tooltips_enabled',
                        lambda *a: calls.append(a))

    editor.set_object(make_brush('one'))
    editor.set_object(make_brush('two'))

    assert calls == []


# ────────────────────────────
# The editor routing the setting to both areas
# ────────────────────────────

class FakeEditorWindow(QWidget):
    """The slice of MainWindow ``apply_tooltip_settings`` touches.

    The real method is bound onto it: the full editor needs a GL context and
    cannot be built here, but this is the code that actually ships.
    """

    from editor.main_window import MainWindow
    apply_tooltip_settings = MainWindow.apply_tooltip_settings
    del MainWindow

    def __init__(self, config):
        super().__init__()
        self.config = config


def _toolbar(qt_app):
    from PyQt5.QtWidgets import QToolBar
    bar = QToolBar()
    button = QPushButton()
    button.setToolTip("Select tool")
    bar.addWidget(button)
    return bar, button


def test_it_applies_both_settings(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Editor')
    config.set('Editor', 'property_editor_tooltips', 'False')
    config.set('Editor', 'toolbar_tooltips', 'False')

    host = FakeEditorWindow(config)
    host.property_editor = PropertyEditor(FakeHost())
    host.property_editor.set_object(make_brush())
    host.tool_toolbar, button = _toolbar(qt_app)

    host.apply_tooltip_settings()

    assert _tooltips(host.property_editor) == []
    assert button.toolTip() == ''


def test_the_two_switches_are_independent(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Editor')
    config.set('Editor', 'property_editor_tooltips', 'True')
    config.set('Editor', 'toolbar_tooltips', 'False')

    host = FakeEditorWindow(config)
    host.property_editor = PropertyEditor(FakeHost())
    host.property_editor.set_object(make_brush())
    host.tool_toolbar, button = _toolbar(qt_app)

    host.apply_tooltip_settings()

    assert _tooltips(host.property_editor)
    assert button.toolTip() == ''


def test_it_defaults_to_showing_both(qt_app):
    host = FakeEditorWindow(configparser.ConfigParser())
    host.property_editor = PropertyEditor(FakeHost())
    host.property_editor.set_object(make_brush())
    host.tool_toolbar, button = _toolbar(qt_app)

    host.apply_tooltip_settings()

    assert _tooltips(host.property_editor)
    assert button.toolTip() == 'Select tool'


def test_it_copes_before_either_area_exists(qt_app):
    """It runs during startup, so neither attribute is guaranteed yet."""
    host = FakeEditorWindow(configparser.ConfigParser())

    host.apply_tooltip_settings()
