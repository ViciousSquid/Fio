"""Held movement keys stay key presses, not text input.

macOS answers a held letter key in a widget that reports an enabled input
method with its press-and-hold accent picker. In Play (and editor fly-look)
W is held to walk, so whichever widget kept keyboard focus -- the outliner, a
dock, the view -- opened a "Ŵ 1" popup over the game. While the view is driven
by the keyboard, every widget but a real text field reports no input method.
"""

from PyQt5.QtCore import QEvent, QObject, Qt
from PyQt5.QtWidgets import QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

#: Widgets that take typed text: the consoles keep their input method.
TEXT_INPUTS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)


def keyboard_drives_view(view) -> bool:
    """Whether held keys move the player or the editor camera right now."""
    return bool(getattr(view, "play_mode", False) or getattr(view, "mouselook_active", False))


class HeldKeyInputMethodFilter(QObject):
    """Answer ``ImEnabled`` with false while the keyboard drives the view."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window

    def eventFilter(self, obj, event):
        if (event.type() != QEvent.InputMethodQuery
                or not event.queries() & Qt.ImEnabled
                or not isinstance(obj, QWidget) or isinstance(obj, TEXT_INPUTS)
                or obj.window() is not self.window
                or not keyboard_drives_view(getattr(self.window, "view_3d", None))):
            return False
        event.setValue(Qt.ImEnabled, False)
        event.accept()
        return True
