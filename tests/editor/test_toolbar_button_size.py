"""Settings > Large Toolbar Buttons is a maximum, not an overflow command.

All editing buttons, including Play/Stop, must fit the toolbar using the
same logical-pixel geometry on Windows and macOS. The large-mode dimensions
are requested caps; smaller windows may produce smaller buttons.
"""

import configparser

import pytest

pytest.importorskip("PyQt5", reason="the toolbar is a Qt widget")

from PyQt5.QtWidgets import QPushButton  # noqa: E402

from editor.ui import (  # noqa: E402
    PLAY_BUTTON_LENGTHS, TOOLBAR_ICON_SIZES, fit_toolbar_metrics, toolbar_metrics,
)

pytestmark = pytest.mark.qt


def test_small_icons_are_ten_percent_larger_without_changing_large_mode():
    """24px * 1.10 rounds to 26 Qt logical pixels on both platforms."""
    assert TOOLBAR_ICON_SIZES[False] == round(24 * 1.10) == 26
    assert TOOLBAR_ICON_SIZES[True] == 40
    assert PLAY_BUTTON_LENGTHS == {False: 110, True: 250}

    config = configparser.ConfigParser()
    config["Display"] = {"big_toolbar_buttons": "False"}
    assert toolbar_metrics(config) == (26, 110)

    # The extra two pixels must still fit a typical vertical editor toolbar.
    icon, play = fit_toolbar_metrics(
        config, available=900, buttons=15, separators=3, vertical=True,
        spacing=4, margin=2, handle=8, separator_extent=8)
    assert (icon, play) == (26, 110)


def _set_big(window, big):
    if not window.config.has_section("Display"):
        window.config.add_section("Display")
    window.config.set("Display", "big_toolbar_buttons", str(big))
    window.apply_toolbar_button_size()


def _icon_buttons(window):
    return [b for b in window.tool_toolbar.findChildren(QPushButton)
            if b.property("toolbar_icon_button")]


@pytest.mark.parametrize("big", [True, False])
def test_the_setting_resizes_every_toolbar_button_without_a_restart(main_window, big):
    _set_big(main_window, big)
    icon = main_window._toolbar_icon_size
    length = main_window._play_button_length
    assert 1 <= icon <= TOOLBAR_ICON_SIZES[big]
    assert 32 <= length <= PLAY_BUTTON_LENGTHS[big]

    buttons = _icon_buttons(main_window)
    assert buttons
    for button in buttons:
        assert button.iconSize().width() == icon
        assert (button.width(), button.height()) == (icon + 4, icon + 8)

    play = main_window.play_button
    # Play is rotated when docked on the right; its long side still follows
    # the measured maximum, never blindly the old 250px request.
    assert max(play.width(), play.height()) == length
    assert min(play.width(), play.height()) == icon + 16


def test_small_mode_never_makes_the_toolbar_larger(main_window, qt_app):
    _set_big(main_window, True)
    qt_app.processEvents()
    large_icon = main_window._toolbar_icon_size
    large_width = main_window.tool_toolbar.sizeHint().width()

    _set_big(main_window, False)
    qt_app.processEvents()
    assert main_window._toolbar_icon_size <= large_icon
    assert main_window.tool_toolbar.sizeHint().width() <= large_width


def test_play_and_stop_keep_the_fitted_length(main_window):
    _set_big(main_window, False)
    play = main_window.play_button
    fitted_length = main_window._play_button_length
    main_window.update_play_button_color()
    assert max(play.width(), play.height()) == fitted_length

    _set_big(main_window, True)
    fitted_length = main_window._play_button_length
    main_window.update_play_button_color()
    assert max(play.width(), play.height()) == fitted_length
