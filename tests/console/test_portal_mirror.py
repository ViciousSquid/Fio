"""The ``portal_mirror`` console command."""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler

pytestmark = pytest.mark.qt


class _State:
    pass


class _View:
    def __init__(self):
        self.play_mode = True
        self.portal_mirror = True
        self.updates = 0

    def update(self):
        self.updates += 1


class _MainWindow:
    def __init__(self):
        self.state = _State()
        self.view_3d = _View()
        self.toasts = []

    def show_toast(self, text):
        self.toasts.append(text)


def test_portal_mirror_0_and_1_switch_the_view_setting():
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command("portal_mirror 0")
    assert window.view_3d.portal_mirror is False
    assert window.toasts[-1] == "Portal mirror: OFF"

    handler.handle_command("portal_mirror 1")
    assert window.view_3d.portal_mirror is True
    assert window.toasts[-1] == "Portal mirror: ON"
    assert window.view_3d.updates == 2


@pytest.mark.parametrize("args", ["portal_mirror", "portal_mirror maybe"])
def test_portal_mirror_without_0_or_1_leaves_the_setting_alone(args):
    window = _MainWindow()
    handler = ConsoleCommandHandler(window)

    handler.handle_command(args)

    assert window.view_3d.portal_mirror is True
    assert window.toasts == []
