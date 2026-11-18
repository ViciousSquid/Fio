"""Slide-in toasts (editor/toasts.py): only loads, saves, warnings and errors.

Tips, hints and routine feedback are not toasted; the bottom bar's
notification area shows nothing. A toast slides in at the top right, stacks,
can be pinned by clicking it, and its text can be selected and copied.
"""

import pytest

pytest.importorskip("PyQt5", reason="toasts are editor-tier")

from PyQt5.QtCore import QPoint, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

pytestmark = pytest.mark.qt


def _shown(window):
    manager = getattr(window, 'toast_manager', None)
    return [] if manager is None else manager.messages()


def test_routine_feedback_and_tips_are_not_toasted(main_window):
    main_window.show_toast("Grid Size: 32")
    main_window.show_toast("FACE MODE: OFF", duration=3000)
    main_window.show_tooltip("Press Z or hold right mouse to look")
    assert _shown(main_window) == []
    assert main_window.ui.notification_label.text() == ""     # bottom right: nothing


def test_loads_saves_warnings_and_errors_are_toasted(main_window):
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("Loaded _SHOWCASE.json", level='success')
    main_window.show_toast("The texture could not be read", level='warning')
    main_window.show_toast("Failed to load level", is_error=True)
    assert _shown(main_window) == [
        ('success', "Saved"), ('success', "Loaded _SHOWCASE.json"),
        ('warning', "The texture could not be read"), ('error', "Failed to load level")]


def test_toasts_stack_at_the_bottom_right_above_the_old_notification_area(main_window):
    main_window.resize(1200, 800)
    QTest.qWait(50)
    first = main_window.show_toast("Saved", level='success')
    second = main_window.show_toast("Failed", is_error=True)
    QTest.qWait(400)                                    # the slide-in
    right = main_window.width() - first.x() - first.width()
    assert 0 < right <= 40                              # at the right edge
    old_area = main_window.ui.notification_label.mapTo(main_window, QPoint(0, 0)).y()
    assert second.y() + second.height() <= old_area     # just above it...
    assert old_area - (second.y() + second.height()) <= 30
    assert first.y() + first.height() <= second.y()     # ...the older one above


def test_a_toast_goes_after_its_duration(main_window):
    main_window.show_toast("Saved", level='success', duration=100)
    QTest.qWait(700)
    assert _shown(main_window) == []


def test_the_same_message_again_does_not_stack(main_window):
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("Saved", level='success')
    assert _shown(main_window) == [('success', "Saved")]


def test_clicking_a_toast_pins_it(main_window):
    toast = main_window.show_toast("Failed to save", is_error=True, duration=150)
    QTest.mouseClick(toast, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    QTest.qWait(600)
    assert _shown(main_window) == [('error', "Failed to save")]   # still there
    toast.close_button.click()                                    # until closed
    QTest.qWait(600)
    assert _shown(main_window) == []


def test_clicking_its_text_pins_it_too(main_window):
    toast = main_window.show_toast("Failed to save", is_error=True, duration=150)
    QTest.mouseClick(toast.label, Qt.LeftButton, Qt.NoModifier, QPoint(3, 3))
    QTest.qWait(600)
    assert toast.pinned and _shown(main_window)


def test_a_toast_s_text_can_be_copied(main_window):
    from PyQt5.QtWidgets import QApplication
    message = "C:/maps/level.json could not be written"
    toast = main_window.show_toast(message, is_error=True)
    # Selectable, a part at a time...
    flags = toast.label.textInteractionFlags()
    assert flags & Qt.TextSelectableByMouse and flags & Qt.TextSelectableByKeyboard
    toast.label.setSelection(0, 7)
    assert toast.label.selectedText() == "C:/maps"
    # ...and all of it with the Copy button.
    QApplication.clipboard().setText("")
    toast.copy_button.click()
    assert QApplication.clipboard().text() == message
    assert toast.pinned                                  # it was wanted: it stays


def test_a_toast_has_no_action_buttons(main_window):
    from PyQt5.QtWidgets import QPushButton
    toast = main_window.show_toast("Saved", level='success')
    assert toast.findChildren(QPushButton) == []        # icon, text, pin, copy, close


def test_every_toast_also_goes_to_the_debug_console(main_window):
    from editor.debug_console import get_debug_logger, console_plain_text
    logger = get_debug_logger()
    before = len(logger.get_buffer())
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("Failed to load level", is_error=True)
    main_window.show_toast("Grid Size: 32")             # not a toast at all
    # Popped up...
    assert _shown(main_window) == [('success', "Saved"), ('error', "Failed to load level")]
    # ...and a console card each, in order; routine feedback is not logged.
    assert [console_plain_text(*entry) for entry in logger.get_buffer()[before:]] == [
        "Saved", "Failed to load level"]


def test_toasts_cannot_be_turned_off(main_window):
    """The "Disable Toast Notifications" setting is gone: nothing reads it,
    and the Settings window no longer has it."""
    from editor.SettingsWindow import SettingsWindow
    if not main_window.config.has_section('Display'):
        main_window.config.add_section('Display')
    main_window.config.set('Display', 'disable_toasts', 'True')     # an old settings.ini
    try:
        main_window.show_toast("Saved", level='success')
        assert _shown(main_window) == [('success', "Saved")]
    finally:
        main_window.config.remove_option('Display', 'disable_toasts')
    dialog = SettingsWindow(main_window.config, None)
    try:
        assert not hasattr(dialog, 'disable_toasts_checkbox')
        from PyQt5.QtWidgets import QCheckBox
        assert not [b for b in dialog.findChildren(QCheckBox) if 'toast' in b.text().lower()]
    finally:
        dialog.deleteLater()


def test_toasts_go_after_three_seconds_and_errors_linger(main_window):
    saved = main_window.show_toast("Saved", level='success')
    warned = main_window.show_toast("Careful", level='warning')
    failed = main_window.show_toast("Failed", is_error=True)
    assert saved.timer.interval() == 3000 and warned.timer.interval() == 3000
    assert failed.timer.interval() > 3000
    QTest.qWait(3600)
    assert _shown(main_window) == [('error', "Failed")]


def test_the_little_x_dismisses_it(main_window):
    toast = main_window.show_toast("Saved", level='success', duration=0)
    assert toast.close_button.text() == "\u2715"
    assert toast.close_button.toolTip() == "Dismiss"
    toast.close_button.click()
    QTest.qWait(500)
    assert _shown(main_window) == []


def test_a_pinned_toast_shows_a_pin_and_unpinning_lets_it_go(main_window):
    toast = main_window.show_toast("Failed to save", is_error=True, duration=150)
    assert toast.pin_button.isHidden()                 # not pinned: no pin
    QTest.mouseClick(toast, Qt.LeftButton, Qt.NoModifier, QPoint(10, 10))
    assert toast.pinned and not toast.pin_button.isHidden()
    pinned_icon = toast.pin_button.icon().pixmap(16, 16).toImage()
    assert "unpin" in toast.pin_button.toolTip().lower()
    QTest.qWait(500)
    assert _shown(main_window)                          # pinned: still there

    toast.pin_button.click()                            # unpin
    assert not toast.pinned
    unpinned_icon = toast.pin_button.icon().pixmap(16, 16).toImage()
    assert unpinned_icon != pinned_icon                 # the unpinned pin
    assert "click to pin" in toast.pin_button.toolTip().lower()
    QTest.qWait(3400)                                   # goes on its own
    assert _shown(main_window) == []


def test_the_unpinned_pin_pins_it_again(main_window):
    toast = main_window.show_toast("Failed to save", is_error=True)
    toast.pin()
    toast.pin_button.click()
    toast.pin_button.click()
    assert toast.pinned
