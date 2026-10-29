"""Console hotkey routing, including an ISO Mac's Section key."""

import pytest

pytest.importorskip("PyQt5")
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from editor.console_key import ConsoleShortcutFilter, is_console_key

pytestmark = pytest.mark.qt


def _key(key=Qt.Key_unknown, text="", scan=0, native=0, repeat=False):
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, scan, native, 0,
                     text, repeat, 1)


@pytest.mark.parametrize("platform,scan,native,key,text", [
    ("darwin", 0, 0x0A, Qt.Key_section, "§"),
    ("darwin", 0, 0x0A, ord("±"), "±"),
    ("darwin", 0, 0x0A, Qt.Key_unknown, ""),
    ("darwin", 0, 0x32, Qt.Key_QuoteLeft, "`"),
    ("win32", 0x29, 0, Qt.Key_unknown, ""),
    ("linux", 49, 0, Qt.Key_unknown, ""),
    ("darwin", 0, 0, Qt.Key_section, "§"),
    ("win32", 0, 0, Qt.Key_AsciiTilde, "~"),
])
def test_physical_and_symbol_console_keys(platform, scan, native, key, text):
    assert is_console_key(_key(key, text, scan, native), platform)


@pytest.mark.parametrize("platform,scan,native,key,text", [
    ("darwin", 0, 0x0B, Qt.Key_B, "b"),
    ("win32", 0x1E, 0, Qt.Key_A, "a"),
    ("linux", 41, 0, Qt.Key_F, "f"),
    ("linux", 38, 0, Qt.Key_A, "a"),
])
def test_other_native_keys_keep_their_normal_actions(platform, scan, native, key, text):
    assert not is_console_key(_key(key, text, scan, native), platform)


def test_key_is_caught_inside_console_input_and_repeat_does_not_toggle(qt_app):
    class Window(QWidget):
        toggles = 0

        def toggle_debug_console(self):
            self.toggles += 1

    window = Window()
    line = QLineEdit(window)
    QVBoxLayout(window).addWidget(line)
    filter_ = ConsoleShortcutFilter(window)
    qt_app.installEventFilter(filter_)
    try:
        line.setText("sim history")
        for repeat in (False, True, True, False):
            qt_app.sendEvent(line, _key(Qt.Key_section, "§", native=0x0A, repeat=repeat))
        assert window.toggles == 2
        assert line.text() == "sim history"
        qt_app.sendEvent(line, _key(Qt.Key_X, "x"))
        assert line.text().endswith("x")
    finally:
        qt_app.removeEventFilter(filter_)
        window.deleteLater()


def test_the_window_toggles_on_the_physical_console_key(main_window, monkeypatch):
    toggles = []
    monkeypatch.setattr(main_window, "toggle_debug_console", lambda: toggles.append(True))
    main_window.keyPressEvent(_key(Qt.Key_section, "§"))
    assert toggles == [True]


def test_section_key_opens_and_closes_the_real_play_console(main_window, qt_app):
    main_window.view_3d.play_mode = True
    try:
        qt_app.sendEvent(main_window.view_3d, _key(Qt.Key_section, "§", native=0x0A))
        assert main_window._is_play_console_visible()
        assert main_window.view_3d.console_overlay_active
        line = main_window.view_3d._console_input
        line.setText("sim history")
        qt_app.sendEvent(line, _key(Qt.Key_section, "§", native=0x0A))
        assert not main_window._is_play_console_visible()
        assert line.text() == "sim history"
    finally:
        main_window.view_3d.play_mode = False
