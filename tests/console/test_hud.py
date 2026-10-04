"""Regression tests for the HUD console controls through the real editor host."""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler

pytestmark = pytest.mark.qt


def _real_handler(window):
    window.config.remove_option("Display", "hudstyle")
    return ConsoleCommandHandler(window)


def test_hudstyle_changes_real_view_without_persisting(main_window):
    handler = _real_handler(main_window)

    handler.handle_command("hudstyle 2")

    assert main_window.view_3d._hud_style == 2
    assert not main_window.config.has_option("Display", "hudstyle")


def test_hudstyle_zero_hides_real_view_without_persisting(main_window):
    handler = _real_handler(main_window)

    handler.handle_command("hudstyle 0")

    assert main_window.view_3d._hud_style == 0
    assert not main_window.config.has_option("Display", "hudstyle")


def test_map_hudstyle_changes_the_real_view_without_persisting(main_window):
    handler = _real_handler(main_window)

    handler.handle_command(
        'hudstyle 4 "LCDAT&TPhoneTimeDate.ttf"', from_map=True
    )

    assert main_window.view_3d._hud_style == 4
    assert main_window.view_3d._hud_font_override == "LCDAT&TPhoneTimeDate.ttf"
    assert not main_window.config.has_option("Display", "hudstyle")


def test_hudopacity_changes_the_real_view_and_persists(main_window):
    handler = _real_handler(main_window)

    handler.handle_command("hudopacity 37.5")

    assert main_window.view_3d._hud_opacity == pytest.approx(37.5)
    assert main_window.config.get("Display", "hudopacity") == "37.5"

    handler.handle_command("hudopacity 101")
    assert main_window.view_3d._hud_opacity == pytest.approx(37.5)


def test_hudfade_updates_the_real_view_logic_and_persists(main_window):
    handler = _real_handler(main_window)

    handler.handle_command("hudfade 0")

    assert main_window.view_3d._hud_fade_enabled is False
    assert main_window.view_3d.logic_thread._hud_fade_enabled is False
    assert main_window.config.getboolean("Display", "hudfade") is False


@pytest.mark.parametrize(
    ("method", "attribute", "expected"),
    [
        ("show_view_message", "message", "Hello world"),
        ("show_view_message2", "message2", "Second line"),
        ("show_view_message3", "message3", "Third line"),
    ],
)
def test_message_commands_reach_real_play_view_overlays(
    monkeypatch, main_window, method, attribute, expected
):
    calls = []
    view = main_window.view_3d
    real = getattr(view, method)

    def observe(text, _real=real):
        calls.append(text)
        return _real(text)

    monkeypatch.setattr(view, method, observe)
    ConsoleCommandHandler(main_window).handle_command(
        f'message{"" if method == "show_view_message" else method[-1]} "{expected}"'
    )

    assert calls == [expected]


def test_hudstyle_is_not_shipped_in_settings_ini():
    from tests.helpers.paths import repo_path

    settings = configparser.ConfigParser()
    settings.read(repo_path("settings.ini"))

    assert not settings.has_option("Display", "hudstyle")


def test_reload_hud_settings_ignores_legacy_hudstyle(main_window):
    view = main_window.view_3d
    config = main_window.config
    config.set("Display", "hudstyle", "4")
    config.set("Display", "hudopacity", "80")
    config.set("Display", "hudfade", "False")

    view._reload_hud_settings()

    assert view._hud_style == 1
    assert view._hud_font_override is None
    assert view._hud_opacity == pytest.approx(80.0)
    assert view._hud_fade_enabled is False
