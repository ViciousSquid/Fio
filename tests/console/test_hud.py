"""Regression tests for the HUD console controls."""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler

pytestmark = pytest.mark.qt


class _Logic:
    def __init__(self):
        self.hud_fade = True

    def set_hud_fade_enabled(self, enabled):
        self.hud_fade = bool(enabled)


class _View:
    def __init__(self):
        self._hud_style = 1
        self._hud_opacity = 100.0
        self._hud_fade_enabled = True
        self.play_mode = True
        self.style_calls = []
        self.opacity_calls = []
        self.fade_calls = []
        self.message_calls = []
        self.message2_calls = []
        self.message3_calls = []
        self.logic_thread = _Logic()

    def set_hud_style(self, style, font_name=None):
        self._hud_style = int(style)
        self.style_calls.append((int(style), font_name))
        return True

    def set_hud_opacity(self, opacity):
        value = float(opacity)
        if not 0.0 <= value <= 100.0:
            return False
        self._hud_opacity = value
        self.opacity_calls.append(value)
        return True

    def set_hud_fade_enabled(self, enabled):
        self._hud_fade_enabled = bool(enabled)
        self.fade_calls.append(bool(enabled))
        self.logic_thread.set_hud_fade_enabled(enabled)
        return True

    def show_view_message(self, text):
        self.message_calls.append(text)

    def show_view_message2(self, text):
        self.message2_calls.append(text)

    def show_view_message3(self, text):
        self.message3_calls.append(text)


class _MainWindow:
    def __init__(self):
        self.state = object()
        self.view_3d = _View()
        self.config = configparser.ConfigParser()
        self.config.add_section("Display")
        self.config.set("Display", "show_hud", "True")
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


def test_hudstyle_changes_runtime_without_persisting():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command("hudstyle 2")

    assert window.view_3d.style_calls == [(2, None)]
    assert not window.config.has_option("Display", "hudstyle")
    assert window.config.getboolean("Display", "show_hud") is True
    assert window.save_count == 0


def test_hudstyle_zero_hides_runtime_without_persisting():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command("hudstyle 0")

    assert window.view_3d.style_calls == [(0, None)]
    assert not window.config.has_option("Display", "hudstyle")
    assert window.config.getboolean("Display", "show_hud") is True
    assert window.save_count == 0


def test_map_hudstyle_is_runtime_only():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command(
        'hudstyle 4 "LCDAT&TPhoneTimeDate.ttf"', from_map=True
    )

    assert window.view_3d.style_calls == [(4, "LCDAT&TPhoneTimeDate.ttf")]
    assert not window.config.has_option("Display", "hudstyle")
    assert window.save_count == 0


def test_hudopacity_changes_runtime_and_persists():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command("hudopacity 37.5")

    assert window.view_3d.opacity_calls == [37.5]
    assert window.config.get("Display", "hudopacity") == "37.5"

    handler.handle_command("hudopacity 101")
    assert window.view_3d.opacity_calls == [37.5]


def test_hudfade_updates_runtime_logic_and_persists():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command("hudfade 0")

    assert window.view_3d._hud_fade_enabled is False
    assert window.view_3d.logic_thread.hud_fade is False
    assert window.config.getboolean("Display", "hudfade") is False


@pytest.mark.parametrize(
    ("command", "attribute", "expected"),
    [
        ("message \"Hello world\"", "message_calls", "Hello world"),
        ("message2 \"Second line\"", "message2_calls", "Second line"),
        ("message3 \"Third line\"", "message3_calls", "Third line"),
    ],
)
def test_message_commands_reach_play_view_overlay(command, attribute, expected):
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command(command)

    assert getattr(window.view_3d, attribute) == [expected]


def test_hudstyle_is_not_shipped_in_settings_ini():
    from tests.helpers.paths import REPO_ROOT

    settings = configparser.ConfigParser()
    settings.read(REPO_ROOT / "settings.ini")

    assert not settings.has_option("Display", "hudstyle")


def test_reload_hud_settings_ignores_legacy_hudstyle():
    from types import SimpleNamespace
    from engine.qt_game_view import QtGameView

    config = configparser.ConfigParser()
    config.add_section("Display")
    config.set("Display", "hudstyle", "4")
    config.set("Display", "hudopacity", "80")
    config.set("Display", "hudfade", "False")

    view = QtGameView.__new__(QtGameView)
    view.editor = SimpleNamespace(config=config)
    view.logic_thread = None
    view._refresh_hud_status_font = lambda: None

    view._reload_hud_settings()

    assert view._hud_style == 1
    assert view._hud_font_override is None
    assert view._hud_opacity == 80.0
    assert view._hud_fade_enabled is False
