"""Regression tests for the transient 3D-view ``message`` console command."""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler

pytestmark = pytest.mark.qt


@pytest.fixture
def shown(monkeypatch, main_window):
    """Record the text each real ``show_view_message*`` line is asked to show."""
    view = main_window.view_3d
    lines = {"1": [], "2": [], "3": []}
    for line, method in (("1", "show_view_message"),
                         ("2", "show_view_message2"),
                         ("3", "show_view_message3")):
        real = getattr(view, method)

        def observe(text, _real=real, _line=line):
            lines[_line].append(str(text).strip()[:50])
            return _real(text)

        monkeypatch.setattr(view, method, observe)
    return lines


def test_message_command_accepts_quoted_text_and_truncates_to_50_chars(main_window, shown):
    main_window.view_3d.play_mode = True
    handler = ConsoleCommandHandler(main_window)

    handler.handle_command('message "Hello, this is a message with spaces."')

    assert shown["1"] == ["Hello, this is a message with spaces."]

    long_text = "x" * 60
    handler.handle_command(f'message "{long_text}"')

    assert shown["1"][-1] == "x" * 50
    assert len(shown["1"][-1]) == 50


def test_message_command_is_play_mode_only(main_window, shown):
    main_window.view_3d.play_mode = False
    handler = ConsoleCommandHandler(main_window)

    handler.handle_command('message "Hello"')

    assert shown["1"] == []


def test_message2_command_uses_the_second_independent_line(main_window, shown):
    main_window.view_3d.play_mode = True
    handler = ConsoleCommandHandler(main_window)

    handler.handle_command('message "First"')
    handler.handle_command('message2 "Second"')

    assert shown["1"] == ["First"]
    assert shown["2"] == ["Second"]


def test_message3_command_uses_the_third_line(main_window, shown):
    main_window.view_3d.play_mode = True
    handler = ConsoleCommandHandler(main_window)

    handler.handle_command('message "First"')
    handler.handle_command('message2 "Second"')
    handler.handle_command('message3 "Rushford"')

    assert shown["1"] == ["First"]
    assert shown["2"] == ["Second"]
    assert shown["3"] == ["Rushford"]
