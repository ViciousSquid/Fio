"""Up repeats the last command, in every console.

The editor's Debug Console had command history; the Play console at the bottom
of the screen was a plain line edit, so Up did nothing there. Every command
input now keeps the same history, and Up starts from the newest command each
time a console opens.
"""

import pytest

pytest.importorskip("PyQt5", reason="the consoles are Qt widgets")

from PyQt5.QtCore import QEvent, Qt                          # noqa: E402
from PyQt5.QtGui import QKeyEvent                            # noqa: E402
from PyQt5.QtWidgets import QApplication                     # noqa: E402

from editor import debug_console                             # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(autouse=True)
def fresh_history():
    saved = list(debug_console.COMMAND_HISTORY)
    debug_console.COMMAND_HISTORY.clear()
    yield
    debug_console.COMMAND_HISTORY[:] = saved


def _press(widget, key):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def _enter(console, command):
    console.command_input.setText(command)
    console._on_command_entered()


def test_up_repeats_the_last_command_in_the_debug_console(main_window):
    console = main_window.debug_console
    _enter(console, "help")
    _enter(console, "fps")
    inp = console.command_input
    _press(inp, Qt.Key_Up)
    assert inp.text() == "fps"
    _press(inp, Qt.Key_Up)
    assert inp.text() == "help"
    _press(inp, Qt.Key_Down)
    assert inp.text() == "fps"


def test_up_repeats_the_last_command_in_the_play_console(main_window):
    view = main_window.view_3d
    view.play_mode = True
    try:
        view._open_console_overlay()
        view._console_input.setText("god")
        view._submit_console_command()           # run through the console, closed

        view._open_console_overlay()
        assert view._console_input.text() == ""
        _press(view._console_input, Qt.Key_Up)
        assert view._console_input.text() == "god"
    finally:
        view._close_console_overlay()
        view.play_mode = False


def test_up_in_the_play_console_repeats_a_command_typed_in_the_debug_console(main_window):
    _enter(main_window.debug_console, "noclip")
    view = main_window.view_3d
    view.play_mode = True
    try:
        view._open_console_overlay()
        _press(view._console_input, Qt.Key_Up)
        assert view._console_input.text() == "noclip"
    finally:
        view._close_console_overlay()
        view.play_mode = False
