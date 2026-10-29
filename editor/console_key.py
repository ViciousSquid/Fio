"""The console lives on the physical key below Escape, across keyboard layouts."""

import sys

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import QWidget


def is_console_key(event, platform=None):
    platform = sys.platform if platform is None else platform
    # Cocoa virtual keycodes: ISO Section (European Macs) and ANSI Grave.
    if platform == "darwin" and event.nativeVirtualKey() in (0x0A, 0x32):
        return True
    scan = event.nativeScanCode()
    if platform == "win32" and scan == 0x29:
        return True
    # Qt's XKB keycode for the grave position (evdev KEY_GRAVE + 8).
    if platform.startswith("linux") and scan == 49:
        return True
    return (event.key() in (Qt.Key_QuoteLeft, Qt.Key_AsciiTilde, Qt.Key_section)
            or event.text() in ("`", "~", "§", "±"))


class ConsoleShortcutFilter(QObject):
    """Catch the key before a text input or a modal game menu consumes it."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if (event.type() not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease)
                or not isinstance(obj, QWidget) or obj.window() is not self.window
                or not is_console_key(event)):
            return False
        event.accept()
        if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
            self.window.toggle_debug_console()
        return True
