"""A disabled plugin's menu actions are hidden, not greyed out.

``_run_plugin_menu_action`` refuses to invoke a plugin-owned action whose
plugin is off, so a visible-but-dead entry is an offer the editor cannot
honour. Tidy's "Load Demo map" looked available with Tidy disabled and silently
did nothing when clicked.
"""

import pytest

pytest.importorskip("PyQt5", reason="the plugins menu is editor-tier")

from PyQt5.QtWidgets import QMainWindow                 # noqa: E402

import plugins.integration as integration               # noqa: E402
from plugins.manager import get_manager, load_plugins   # noqa: E402

pytestmark = pytest.mark.qt


def plugin_action_items(window, plugin_name):
    """The (label, visible, enabled) triples for one plugin's own actions."""
    for action in window.menuBar().actions():
        if action.text().replace("&", "") != "Plugins":
            continue
        for sub in action.menu().actions():
            if sub.text() != plugin_name or sub.menu() is None:
                continue
            items = []
            for entry in sub.menu().actions():
                if entry.text() == "Enabled":
                    break          # plugin-owned actions all sit above this
                items.append((entry.text(), entry.isVisible(), entry.isEnabled()))
            return items
    return []


@pytest.fixture
def window(qt_app):
    win = QMainWindow()
    yield win
    win.close()
    win.deleteLater()


def _tidy():
    load_plugins()
    mgr = get_manager()
    tidy = mgr.find_plugin("tidy")
    if tidy is None:
        pytest.skip("the Tidy plugin is not present in this build")
    return mgr, tidy


def test_a_disabled_plugins_actions_are_hidden(window):
    mgr, tidy = _tidy()
    was = mgr.is_enabled(tidy)
    try:
        mgr.set_enabled(tidy, False)
        integration._build_plugins_menu(window)
        items = plugin_action_items(window, tidy.name)
        assert items, "Tidy registered no menu actions; this test is looking in the wrong place"
        for label, visible, _enabled in items:
            assert not visible, (
                "'%s' is visible with Tidy disabled; clicking it does nothing"
                % label)
    finally:
        mgr.set_enabled(tidy, was)


def test_an_enabled_plugins_actions_are_shown(window):
    mgr, tidy = _tidy()
    was = mgr.is_enabled(tidy)
    try:
        mgr.set_enabled(tidy, True)
        integration._build_plugins_menu(window)
        items = plugin_action_items(window, tidy.name)
        assert any(label.startswith("Load Demo map") for label, _v, _e in items), (
            "Tidy's demo-map action is missing entirely: %r" % (items,))
        for label, visible, enabled in items:
            assert visible and enabled, (
                "'%s' is hidden or greyed with Tidy enabled" % label)
    finally:
        mgr.set_enabled(tidy, was)


def test_toggling_the_plugin_flips_its_actions_without_a_rebuild(window):
    """The Enabled checkbox updates the entries already on screen."""
    mgr, tidy = _tidy()
    was = mgr.is_enabled(tidy)
    try:
        mgr.set_enabled(tidy, True)
        integration._build_plugins_menu(window)
        before = plugin_action_items(window, tidy.name)
        assert all(v for _l, v, _e in before)

        integration._toggle_plugin(window, tidy, False,
                                   _live_actions(window, tidy.name))
        after = plugin_action_items(window, tidy.name)
        assert not any(v for _l, v, _e in after), (
            "toggling Tidy off left its actions on screen: %r" % (after,))
    finally:
        mgr.set_enabled(tidy, was)


def _live_actions(window, plugin_name):
    for action in window.menuBar().actions():
        if action.text().replace("&", "") != "Plugins":
            continue
        for sub in action.menu().actions():
            if sub.text() == plugin_name and sub.menu() is not None:
                out = []
                for entry in sub.menu().actions():
                    if entry.text() == "Enabled":
                        break
                    out.append(entry)
                return out
    return []
