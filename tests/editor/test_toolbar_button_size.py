"""Settings > Large Toolbar Buttons resizes the toolbar, Play included, at once.

It used to need a restart, and even then the Play button -- the biggest thing
on the toolbar -- stayed 250 pixels long either way, so turning large buttons
off did not visibly make the toolbar smaller.
"""

import pytest

pytest.importorskip("PyQt5", reason="the toolbar is a Qt widget")

from PyQt5.QtWidgets import QPushButton                    # noqa: E402

from editor.ui import PLAY_BUTTON_LENGTHS, TOOLBAR_ICON_SIZES   # noqa: E402

pytestmark = pytest.mark.qt


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
    icon = TOOLBAR_ICON_SIZES[big]
    buttons = _icon_buttons(main_window)
    assert buttons
    for b in buttons:
        assert b.iconSize().width() == icon
        assert (b.width(), b.height()) == (icon + 4, icon + 8)
    play = main_window.play_button
    assert (play.width(), play.height()) == (PLAY_BUTTON_LENGTHS[big], icon + 16)


def test_small_buttons_make_the_toolbar_smaller(main_window, qt_app):
    _set_big(main_window, True)
    qt_app.processEvents()
    big_width = main_window.tool_toolbar.sizeHint().width()
    _set_big(main_window, False)
    qt_app.processEvents()
    assert main_window.tool_toolbar.sizeHint().width() < big_width * 0.85


def test_play_and_stop_keep_the_set_length(main_window):
    _set_big(main_window, False)
    main_window.update_play_button_color()
    assert main_window.play_button.width() == PLAY_BUTTON_LENGTHS[False]
