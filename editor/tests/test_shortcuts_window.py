"""Tests for Help > Keys.

The point of the listing is that it cannot go stale, so most of these are
about where the entries come from: menus and toolbar buttons are read off the
widgets, the config file supplies the editable function keys and the user's
own bindings, and only the keyPressEvent chains are written down.
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtGui import QFont, QKeySequence  # noqa: E402
from PyQt5.QtWidgets import (  # noqa: E402
    QAction, QApplication, QMainWindow, QPushButton, QShortcut,
)

from editor import shortcuts as sc  # noqa: E402
from editor.shortcuts_window import ShortcutsWindow  # noqa: E402
from editor.tooltips import set_tooltips_enabled  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    """A stand-in editor with one of each kind of shortcut."""
    win = QMainWindow()
    bar = win.menuBar()

    file_menu = bar.addMenu('File')
    file_menu.addAction(QAction('New Map', win, shortcut='Ctrl+N'))
    file_menu.addAction(QAction('&Save', win, shortcut='Ctrl+S'))

    edit_menu = bar.addMenu('Edit')
    edit_menu.addAction(QAction('Hide Brush', win, shortcut='H'))

    select_menu = bar.addMenu('Select')
    select_menu.addAction(QAction('Select Touching', win, shortcut='Ctrl+T'))

    button = QPushButton(win)
    button.setShortcut('Shift+A')
    button.setToolTip("Select tool\nsecond line is ignored")
    win.select_button = button

    named = QShortcut(QKeySequence('Ctrl+Tab'), win)
    named.setObjectName('Cycle the 2D view')

    unnamed = QShortcut(QKeySequence('Ctrl+Alt+Z'), win)
    win._shortcuts = (named, unnamed)

    win.config = configparser.ConfigParser()
    return win


def _find(grouped, keys):
    for shortcuts in grouped.values():
        for shortcut in shortcuts:
            if shortcut.keys.lower() == keys.lower():
                return shortcut
    return None


def _all(grouped):
    return [s for shortcuts in grouped.values() for s in shortcuts]


# ────────────────────────────
# Where entries come from
# ────────────────────────────

def test_menu_shortcuts_are_discovered(window):
    grouped = sc.collect(window)

    entry = _find(grouped, 'Ctrl+N')
    assert entry is not None
    assert entry.description == 'New Map'
    assert entry.source == 'menu'


def test_menu_labels_lose_their_ampersands(window):
    grouped = sc.collect(window)

    assert _find(grouped, 'Ctrl+S').description == 'Save'


def test_actions_are_filed_under_their_menu(window):
    grouped = sc.collect(window)

    assert _find(grouped, 'Ctrl+N').category == 'File'
    assert _find(grouped, 'H').category == 'Edit'
    assert _find(grouped, 'Ctrl+T').category == 'Selection'


def test_toolbar_buttons_are_discovered_by_their_tooltip(window):
    grouped = sc.collect(window)

    entry = _find(grouped, 'Shift+A')
    assert entry is not None
    assert entry.description == 'Select tool'      # first line only
    assert entry.source == 'toolbar'


def test_a_named_qshortcut_is_listed(window):
    grouped = sc.collect(window)

    assert _find(grouped, 'Ctrl+Tab').description == 'Cycle the 2D view'


def test_an_unnamed_qshortcut_is_left_to_the_declared_list(window):
    """It has no label, so listing it would print a blank action."""
    grouped = sc.collect(window)

    assert _find(grouped, 'Ctrl+Alt+Z') is None


def test_declared_shortcuts_fill_in_the_keypressevent_chains(window):
    grouped = sc.collect(window)

    entry = _find(grouped, 'Shift+V')
    assert entry is not None
    assert entry.category == 'Component modes'
    assert entry.source == 'built-in'


# ────────────────────────────
# Discovery beats declaration
# ────────────────────────────

def test_a_real_binding_wins_over_the_declared_description(window):
    """H is in DECLARED and also a menu item; the menu is authoritative."""
    assert any(keys == 'H' for _, keys, _ in sc.DECLARED)

    grouped = sc.collect(window)
    matches = [s for s in _all(grouped) if s.keys == 'H']

    assert len(matches) == 1
    assert matches[0].description == 'Hide Brush'


def test_nothing_is_listed_twice(window):
    grouped = sc.collect(window)
    seen = [(s.keys.lower(), s.description.lower()) for s in _all(grouped)]

    assert len(seen) == len(set(seen))


# ────────────────────────────
# The config, including custom bindings
# ────────────────────────────

def test_the_editable_function_keys_show_their_defaults(window):
    grouped = sc.collect(window)

    assert _find(grouped, 'F5').description == 'Enter play mode'


def test_a_rebound_function_key_shows_its_new_key(window):
    window.config.add_section('Shortcuts')
    window.config.set('Shortcuts', 'key_play_mode', 'F8')

    grouped = sc.collect(window)

    assert _find(grouped, 'F8').description == 'Enter play mode'
    assert _find(grouped, 'F5') is None


def test_custom_key_bindings_are_listed(window):
    window.config.add_section('KeyBindings')
    window.config.set('KeyBindings', 'Ctrl+9', 'noclip')

    grouped = sc.collect(window)

    entry = _find(grouped, 'Ctrl+9')
    assert entry is not None
    assert entry.description == 'Console command: noclip'
    assert entry.category == 'Custom bindings'


def test_custom_bindings_are_spelled_the_way_qt_spells_them(window):
    """configparser lower-cases its keys, so "Ctrl+9" arrives as "ctrl+9"."""
    window.config.add_section('KeyBindings')
    window.config.set('KeyBindings', 'Ctrl+9', 'noclip')

    grouped = sc.collect(window)

    assert [s.keys for s in grouped['Custom bindings']] == ['Ctrl+9']


def test_an_unparseable_binding_still_shows_its_text(window):
    window.config.add_section('KeyBindings')
    window.config.set('KeyBindings', 'not-a-key', 'god')

    grouped = sc.collect(window)

    assert _find(grouped, 'not-a-key') is not None


def test_a_window_with_no_config_still_lists_the_built_ins(qt_app):
    grouped = sc.collect(QMainWindow())

    assert _find(grouped, 'Shift+V') is not None


def test_no_window_at_all_is_not_an_error():
    assert sc.collect(None, configparser.ConfigParser())


# ────────────────────────────
# Grouping and ordering
# ────────────────────────────

def test_categories_come_out_in_the_declared_order(window):
    grouped = sc.collect(window)
    positions = [sc.ORDER.index(c) for c in grouped if c in sc.ORDER]

    assert positions == sorted(positions)


def test_no_category_is_empty(window):
    grouped = sc.collect(window)

    assert all(shortcuts for shortcuts in grouped.values())


def test_every_declared_category_is_one_the_window_orders():
    """A typo in DECLARED would otherwise sort a whole group to the bottom."""
    assert {category for category, _, _ in sc.DECLARED} <= set(sc.ORDER)


def test_the_text_export_covers_every_entry(window):
    grouped = sc.collect(window)

    text = sc.as_text(grouped)

    for shortcut in _all(grouped):
        assert shortcut.keys in text
        assert shortcut.description in text
    for category in grouped:
        assert category in text


# ────────────────────────────
# The window
# ────────────────────────────

def test_it_lists_every_shortcut(window, qt_app):
    panel = ShortcutsWindow(window)
    grouped = sc.collect(window)

    rows = 0
    for i in range(panel.tree.topLevelItemCount()):
        rows += panel.tree.topLevelItem(i).childCount()

    assert rows == len(_all(grouped))
    assert panel.tree.topLevelItemCount() == len(grouped)


def test_the_count_is_shown(window, qt_app):
    panel = ShortcutsWindow(window)

    assert panel.count_label.text() == '%d shortcuts' % len(_all(sc.collect(window)))


def test_filtering_matches_the_key(window, qt_app):
    panel = ShortcutsWindow(window)

    panel.search.setText('Ctrl+N')

    visible = _visible_rows(panel)
    assert [r.text(1) for r in visible] == ['New Map']


def test_filtering_matches_the_description(window, qt_app):
    panel = ShortcutsWindow(window)

    panel.search.setText('hide brush')

    assert [r.text(0) for r in _visible_rows(panel)] == ['H']


def test_filtering_hides_the_categories_it_empties(window, qt_app):
    panel = ShortcutsWindow(window)

    panel.search.setText('Ctrl+N')

    groups = [panel.tree.topLevelItem(i)
              for i in range(panel.tree.topLevelItemCount())]
    assert [g.text(0) for g in groups if not g.isHidden()] == ['FILE']


def test_clearing_the_filter_brings_everything_back(window, qt_app):
    panel = ShortcutsWindow(window)
    before = len(_visible_rows(panel))

    panel.search.setText('Ctrl+N')
    panel.search.setText('')

    assert len(_visible_rows(panel)) == before


def test_the_count_reflects_the_filter(window, qt_app):
    panel = ShortcutsWindow(window)
    total = len(_all(sc.collect(window)))

    panel.search.setText('Ctrl+N')

    assert panel.count_label.text() == '1 of %d shortcuts' % total


def test_reopening_picks_up_a_rebound_key(window, qt_app):
    """showEvent re-reads, so Settings changes are not stale on reopen."""
    panel = ShortcutsWindow(window)
    assert _find(panel._grouped, 'F5') is not None

    window.config.add_section('Shortcuts')
    window.config.set('Shortcuts', 'key_play_mode', 'F8')
    panel.show()

    assert _find(panel._grouped, 'F8') is not None
    assert _find(panel._grouped, 'F5') is None


def test_it_is_a_real_window(window, qt_app):
    """Asked for as an OS window: title bar, resizable, not a popup."""
    from PyQt5.QtCore import Qt

    panel = ShortcutsWindow(window)

    assert panel.windowTitle() == 'Keyboard Shortcuts'
    assert panel.windowFlags() & Qt.Window


def test_copying_puts_the_listing_on_the_clipboard(window, qt_app):
    panel = ShortcutsWindow(window)

    panel._copy()

    assert QApplication.clipboard().text() == sc.as_text(panel._grouped)


def _visible_rows(panel):
    rows = []
    for i in range(panel.tree.topLevelItemCount()):
        group = panel.tree.topLevelItem(i)
        for j in range(group.childCount()):
            row = group.child(j)
            if not row.isHidden():
                rows.append(row)
    return rows


# ────────────────────────────
# Interaction with the tooltip switches
# ────────────────────────────

def test_toolbar_buttons_keep_their_names_when_tooltips_are_off(window):
    """Settings > Editor > Tooltips blanks them; the stash still has the text."""
    set_tooltips_enabled(window.select_button, False)
    assert window.select_button.toolTip() == ''

    grouped = sc.collect(window)

    assert _find(grouped, 'Shift+A').description == 'Select tool'


# ────────────────────────────
# Help > Keys
# ────────────────────────────

def test_the_editor_has_the_handler_the_menu_calls(qt_app):
    """ui.py wires Help > Keys to this by reference: a rename breaks startup."""
    from editor.main_window import MainWindow

    assert callable(MainWindow.show_shortcuts_window)


def test_the_help_menu_offers_keys(qt_app):
    """The action ui.py builds, checked without standing up the whole editor.

    Read from the file rather than through inspect.getsource: a plugin wraps
    create_menu_bar at import time, and inspect would hand back the wrapper.
    """
    import editor.ui

    source = open(editor.ui.__file__, encoding='utf-8').read()

    assert "'Keys...'" in source
    assert 'show_shortcuts_window' in source
    assert 'help_menu.addAction(keys_action)' in source


class FakeEditorWindow(QMainWindow):
    """Enough of MainWindow for the Help > Keys handler, which is bound on."""

    from editor.main_window import MainWindow
    show_shortcuts_window = MainWindow.show_shortcuts_window
    del MainWindow

    def __init__(self):
        super().__init__()
        self.config = configparser.ConfigParser()
        self.shortcuts_window = None


def test_opening_it_creates_the_window(qt_app):
    host = FakeEditorWindow()

    host.show_shortcuts_window()

    assert isinstance(host.shortcuts_window, ShortcutsWindow)
    assert host.shortcuts_window.isVisible()


def test_opening_it_twice_reuses_the_same_window(qt_app):
    """Otherwise every visit to the menu stacks another copy."""
    host = FakeEditorWindow()

    host.show_shortcuts_window()
    first = host.shortcuts_window
    host.shortcuts_window.close()
    host.show_shortcuts_window()

    assert host.shortcuts_window is first
    assert first.isVisible()


def test_the_window_lists_the_editor_that_opened_it(qt_app):
    host = FakeEditorWindow()
    host.menuBar().addMenu('File').addAction(
        QAction('New Map', host, shortcut='Ctrl+N'))

    host.show_shortcuts_window()

    assert _find(host.shortcuts_window._grouped, 'Ctrl+N') is not None


# ────────────────────────────
# It has to match the dark theme
# ────────────────────────────

def test_the_tree_carries_its_own_dark_styling(window, qt_app):
    """main.dark_stylesheet styles QWidget but never item views.

    Left to itself a QTreeWidget paints Qt's light defaults -- a white
    header and near-white alternating rows -- straight through the dark
    editor around it.
    """
    from editor import shortcuts_window as sw

    panel = ShortcutsWindow(window)
    sheet = panel.tree.styleSheet()

    assert sw._BG in sheet
    assert 'alternate-background-color' in sheet
    assert 'QHeaderView::section' in sheet


def test_category_rows_are_headings_not_entries(window, qt_app):
    """They must not be selectable, or filtering picks them as results."""
    from PyQt5.QtCore import Qt

    panel = ShortcutsWindow(window)
    group = panel.tree.topLevelItem(0)

    assert not group.flags() & Qt.ItemIsSelectable
    assert group.font(0).bold()


def test_keys_are_set_in_a_fixed_pitch_face(window, qt_app):
    """So modifiers line up down the column instead of ragging."""
    panel = ShortcutsWindow(window)
    row = panel.tree.topLevelItem(0).child(0)

    assert row.font(0).styleHint() == QFont.Monospace
