"""Settings > Appearance HUD font selection and persistent HUD font defaults."""

import configparser
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from engine.hud_fonts import HUD_FONT_FILES, HUD_FONT_FALLBACKS, HUD_FONT_LABELS  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt5", reason="Qt is not available in this environment")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _settings(config):
    from editor.SettingsWindow import SettingsWindow
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        return SettingsWindow(config)
    finally:
        os.chdir(cwd)


def _appearance_tab(dialog):
    for i in range(dialog.tabs.count()):
        if dialog.tabs.tabText(i) == "Appearance":
            return dialog.tabs.widget(i)
    return None


@pytest.mark.qt
def test_appearance_tab_previews_every_hud_font(qt_app):
    dialog = _settings(configparser.ConfigParser())
    assert _appearance_tab(dialog) is not None

    buttons = dialog.hud_font_buttons.buttons()
    assert [b.property("hud_font_style") for b in buttons] == list(HUD_FONT_FILES)
    assert [b.toolTip() for b in buttons] == [
        HUD_FONT_LABELS[style] for style in HUD_FONT_FILES
    ]
    assert all(b.text() == "123 456 789" for b in buttons)
    assert dialog.selected_hud_font() == HUD_FONT_FILES[1]


@pytest.mark.qt
def test_hud_font_choice_round_trips_through_settings_ini(qt_app):
    config = configparser.ConfigParser()
    config.add_section("Display")
    config.set("Display", "hudfont", HUD_FONT_FILES[3])

    dialog = _settings(config)
    assert dialog.selected_hud_font() == HUD_FONT_FILES[3]

    dialog._hud_font_button_for[1].click()
    dialog._save_settings()
    assert config.get("Display", "hudfont") == HUD_FONT_FILES[1]


@pytest.mark.qt
def test_hud_font_default_is_runtime_overridable_by_hud_style(qt_app):
    from engine.qt_game_view import QtGameView

    config = configparser.ConfigParser()
    config.add_section("Display")
    config.set("Display", "hudfont", HUD_FONT_FILES[2])

    view = QtGameView.__new__(QtGameView)
    view.editor = type("Editor", (), {"config": config})()
    view._hud_font_families = dict(HUD_FONT_FALLBACKS)
    view._hud_style = 1
    view._hud_opacity = 100.0
    view._hud_fade_enabled = True
    view._hud_font_override = None
    view._hud_runtime_visible = None
    view._refresh_hud_status_font = lambda: None
    view.update = lambda: None

    QtGameView._reload_hud_settings(view)

    assert view._hud_style == 4
    assert view._hud_font_override == HUD_FONT_FALLBACKS[2]

    assert QtGameView.set_hud_style(view, 3)
    assert view._hud_style == 3
    assert view._hud_font_override is None
