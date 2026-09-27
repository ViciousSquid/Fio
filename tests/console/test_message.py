"""Regression tests for the transient 3D-view ``message`` console command."""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler

pytestmark = pytest.mark.qt


class _State:
    pass


class _View:
    def __init__(self, play_mode=True):
        self.play_mode = play_mode
        self.messages = []
        self.messages2 = []

    def show_view_message(self, text):
        self.messages.append(text)

    def show_view_message2(self, text):
        self.messages2.append(text)


class _MainWindow:
    def __init__(self, play_mode=True):
        self.state = _State()
        self.view_3d = _View(play_mode=play_mode)


def test_message_command_accepts_quoted_text_and_truncates_to_50_chars():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command('message "Hello, this is a message with spaces."')

    assert window.view_3d.messages == ["Hello, this is a message with spaces."]

    long_text = "x" * 60
    handler.handle_command(f'message "{long_text}"')

    assert window.view_3d.messages[-1] == "x" * 50
    assert len(window.view_3d.messages[-1]) == 50


def test_message_command_is_play_mode_only():
    window = _MainWindow(play_mode=False)
    handler = ConsoleCommandHandler(window)

    handler.handle_command('message "Hello"')

    assert window.view_3d.messages == []

def test_message2_command_uses_the_second_independent_line():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command('message "First"')
    handler.handle_command('message2 "Second"')

    assert window.view_3d.messages == ["First"]
    assert window.view_3d.messages2 == ["Second"]
