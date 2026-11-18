"""Toasts: cards in the Debug Console, for important things only.

Only loads and saves (success), warnings and errors are toasted; tips, hints
and routine feedback are not, and there is no pop-up -- a toast is its card
in the console. A card turns faint after a few seconds (it is never removed
by itself), comes back to full colour with a tiny close button when hovered
or clicked, and the close button removes it.
"""

import pytest

pytest.importorskip("PyQt5", reason="toasts are editor-tier")

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from editor import debug_console as dc  # noqa: E402

pytestmark = pytest.mark.qt


def _toasts():
    return [(level, text) for _serial, level, text in dc.get_debug_logger().toasts()]


@pytest.fixture
def console(main_window, monkeypatch):
    monkeypatch.setattr(dc, 'TOAST_FADE_SECONDS', 0.4)
    widget = main_window.debug_console
    widget.clear()
    widget.resize(900, 400)
    widget.show()
    return widget


def _card_colour(console, serial):
    """The background of toast *serial*'s card body, as drawn."""
    table = console._toast_table(serial)
    return table.cellAt(0, 1).format().background().color().name()


def _mouse(console, kind, point):
    viewport = console.console.viewport()
    button = Qt.LeftButton if kind == QEvent.MouseButtonPress else Qt.NoButton
    event = QMouseEvent(kind, QPointF(point), button, button, Qt.NoModifier)
    console.eventFilter(viewport, event)


def _point_on(console, serial):
    from PyQt5.QtGui import QTextCursor
    table = console._toast_table(serial)
    cursor = QTextCursor(console.console.document())
    cursor.setPosition(table.cellAt(0, 1).firstCursorPosition().position() + 2)
    return console.console.cursorRect(cursor).center()


def test_routine_feedback_and_tips_are_not_toasted(main_window, console):
    main_window.show_toast("Grid Size: 32")
    main_window.show_toast("FACE MODE: OFF", duration=3000)
    main_window.show_tooltip("Press Z or hold right mouse to look")
    assert _toasts() == []
    assert main_window.ui.notification_label.text() == ""     # bottom right: nothing


def test_loads_saves_warnings_and_errors_are_console_cards(main_window, console):
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("The texture could not be read", level='warning')
    main_window.show_toast("Failed to load level", is_error=True)
    QTest.qWait(200)
    assert _toasts() == [('success', "Saved"),
                         ('warning', "The texture could not be read"),
                         ('error', "Failed to load level")]
    assert console.console.toHtml().count("<table") == 3


def test_there_is_no_pop_up_toast(main_window, console):
    """Reported: a map load showed its console card and a pop-up toast too."""
    from PyQt5 import sip
    from PyQt5.QtWidgets import QWidget
    # By the C++ widget: Qt can hand back a fresh Python wrapper (a new
    # id()) for a widget that was there all along.
    before = {sip.unwrapinstance(w) for w in main_window.findChildren(QWidget)}
    main_window.show_toast("Loaded _SHOWCASE.json", level='success')
    QTest.qWait(400)
    new = [w for w in main_window.findChildren(QWidget)
           if sip.unwrapinstance(w) not in before]
    assert not [w for w in new if w.isVisible()]          # nothing popped up
    assert not hasattr(main_window, 'toast_manager')
    import importlib.util
    assert importlib.util.find_spec('editor.toasts') is None


def test_a_card_turns_faint_and_stays(main_window, console):
    main_window.show_toast("Saved", level='success')
    QTest.qWait(150)
    serial = dc.get_debug_logger().toasts()[-1][0]
    bright = _card_colour(console, serial)
    QTest.qWait(900)
    faint = _card_colour(console, serial)
    assert faint != bright                                  # faint now...
    assert _toasts() == [('success', "Saved")]              # ...not removed
    assert "Saved" in console.console.toPlainText()


class _Clock:
    """time.monotonic, stepped by the test: the card's wait and its fades
    are measured by it, so nothing here depends on how fast this machine is."""

    def __init__(self, monkeypatch):
        import time
        self.now = time.monotonic()
        monkeypatch.setattr(time, 'monotonic', lambda: self.now)

    def advance(self, console, seconds, steps=10):
        """*seconds* pass, the console's timers ticking *steps* times."""
        for _ in range(steps):
            self.now += seconds / steps
            console._fade_toasts()
            console._ease_toasts()


def test_hover_or_click_highlights_it_with_a_close_button(main_window, console, monkeypatch):
    clock = _Clock(monkeypatch)
    main_window.show_toast("Failed to save", is_error=True)
    QTest.qWait(150)                                         # drawn
    serial = dc.get_debug_logger().toasts()[-1][0]
    clock.advance(console, 1.5)                              # its wait, then the fade
    faint = _card_colour(console, serial)
    _mouse(console, QEvent.MouseMove, _point_on(console, serial))
    assert serial in console._toast_highlighted
    # It lights up over TOAST_TRANSITION_SECONDS (0.5 s), not in one jump.
    assert _card_colour(console, serial) == faint           # not yet
    clock.advance(console, 0.25, steps=3)
    halfway = _card_colour(console, serial)
    assert halfway != faint, "it should be lighting up"
    clock.advance(console, 0.3, steps=3)
    full = _card_colour(console, serial)
    assert full not in (faint, halfway)                     # full colour again
    console.eventFilter(console.console.viewport(), QEvent(QEvent.Leave))
    assert serial not in console._toast_highlighted
    assert _card_colour(console, serial) == full            # no jump back either
    clock.advance(console, 1.5)                              # its wait, then the fade
    assert _card_colour(console, serial) == faint           # faint again
    _mouse(console, QEvent.MouseButtonPress, _point_on(console, serial))
    assert serial in console._toast_highlighted             # a click too


def test_the_close_button_removes_the_card(main_window, console):
    from PyQt5.QtCore import QUrl
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("Failed to save", is_error=True)
    QTest.qWait(200)
    serial = dc.get_debug_logger().toasts()[-1][0]
    link = QUrl(f"closetoast:{serial}")
    console._on_anchor_clicked(link)                         # not showing: no-op
    assert len(_toasts()) == 2
    _mouse(console, QEvent.MouseMove, _point_on(console, serial))
    assert "closetoast:%s" % serial in console.console.toHtml()
    console._on_anchor_clicked(link)                         # highlighted: removes
    assert _toasts() == [('success', "Saved")]
    assert "Failed to save" not in console.console.toPlainText()
    assert console.console.toHtml().count("<table") == 1


def test_a_card_dims_again_with_a_fade_not_a_jump(main_window, console, monkeypatch):
    """Turning faint (after its wait, or once no longer hovered) eases over
    TOAST_TRANSITION_SECONDS too, and the easing stops when it is done."""
    clock = _Clock(monkeypatch)
    main_window.show_toast("Saved", level='success')
    QTest.qWait(150)
    serial = dc.get_debug_logger().toasts()[-1][0]
    full = _card_colour(console, serial)
    console._toast_faint.add(serial)
    console._restyle_toast(serial)
    assert _card_colour(console, serial) == full            # no jump
    clock.advance(console, 0.25, steps=3)
    between = _card_colour(console, serial)
    assert between != full
    clock.advance(console, 0.3, steps=3)
    faint = _card_colour(console, serial)
    assert faint not in (full, between)
    assert not console._toast_easing.isActive()             # stops when done


def test_the_close_button_is_hidden_until_highlighted(main_window, console):
    main_window.show_toast("Saved", level='success')
    QTest.qWait(150)
    serial = dc.get_debug_logger().toasts()[-1][0]
    border, ink, body, close = console._toast_colours('success', 'new')
    assert close == body                                     # drawn in the card's colour
    border, ink, body, close = console._toast_colours('success', 'highlighted')
    assert close == ink


def test_an_error_card_can_be_copied(main_window, console):
    import re
    from PyQt5.QtCore import QUrl
    from PyQt5.QtWidgets import QApplication
    main_window.show_toast("C:/maps/level.json could not be written", is_error=True)
    QTest.qWait(200)
    link = re.search(r'href="(copytoast:\d+)"', console.console.toHtml()).group(1)
    QApplication.clipboard().setText("")
    console._on_anchor_clicked(QUrl(link))
    assert QApplication.clipboard().text() == "C:/maps/level.json could not be written"


def test_toasts_cannot_be_turned_off(main_window, console):
    from editor.SettingsWindow import SettingsWindow
    if not main_window.config.has_section('Display'):
        main_window.config.add_section('Display')
    main_window.config.set('Display', 'disable_toasts', 'True')     # an old settings.ini
    try:
        main_window.show_toast("Saved", level='success')
        assert _toasts() == [('success', "Saved")]
    finally:
        main_window.config.remove_option('Display', 'disable_toasts')
    dialog = SettingsWindow(main_window.config, None)
    try:
        from PyQt5.QtWidgets import QCheckBox
        assert not [b for b in dialog.findChildren(QCheckBox) if 'toast' in b.text().lower()]
    finally:
        dialog.deleteLater()


# -- the same toast again, quickly: a multiplier, not another card ----------

def _card_text(console, serial):
    table = console._toast_table(serial)
    cell = table.cellAt(0, 1)
    from PyQt5.QtGui import QTextCursor
    cursor = cell.firstCursorPosition()
    cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
    return cursor.selectedText()


def test_repeats_show_a_multiplier_not_new_cards(main_window, console):
    for _ in range(3):                                       # before it is drawn
        main_window.show_toast("Saved", level='success')
    QTest.qWait(200)
    serial = dc.get_debug_logger().toasts()[-1][0]
    assert _toasts() == [('success', "Saved")]
    assert console.console.toHtml().count("<table") == 1
    assert "×3" in _card_text(console, serial)
    for _ in range(2):                                       # drawn: updated in place
        main_window.show_toast("Saved", level='success')
    QTest.qWait(100)
    assert _toasts() == [('success', "Saved")]
    assert console.console.toHtml().count("<table") == 1
    text = _card_text(console, serial)
    assert "×5" in text and "×3" not in text
    assert text.startswith("Saved")


def test_a_repeat_after_a_pause_is_a_new_card(main_window, console, monkeypatch):
    monkeypatch.setattr(dc, 'TOAST_REPEAT_SECONDS', 0.2)
    main_window.show_toast("Saved", level='success')
    QTest.qWait(400)
    main_window.show_toast("Saved", level='success')
    QTest.qWait(200)
    assert _toasts() == [('success', "Saved"), ('success', "Saved")]
    assert console.console.toHtml().count("<table") == 2
    assert "×" not in console.console.toPlainText()


def test_other_words_or_another_level_are_their_own_cards(main_window, console):
    main_window.show_toast("Saved", level='success')
    main_window.show_toast("Saved", level='warning')
    main_window.show_toast("Saved twice", level='success')
    main_window.show_toast("Saved", level='success')
    QTest.qWait(200)
    assert _toasts() == [('success', "Saved"), ('warning', "Saved"),
                         ('success', "Saved twice")]
    first = dc.get_debug_logger().toasts()[0][0]
    assert "×2" in _card_text(console, first)


def test_a_repeat_brings_a_faint_card_back_to_full_colour(main_window, console):
    main_window.show_toast("Failed to save", is_error=True)
    QTest.qWait(150)
    serial = dc.get_debug_logger().toasts()[-1][0]
    bright = _card_colour(console, serial)
    QTest.qWait(900)                                          # faint (fade 0.4 s)
    assert _card_colour(console, serial) != bright
    dc.get_debug_logger()._toast_recent[('error', "Failed to save")][1] = \
        __import__('time').monotonic()                       # still "quickly"
    main_window.show_toast("Failed to save", is_error=True)
    QTest.qWait(50)
    assert _card_colour(console, serial) == bright
    assert "×2" in _card_text(console, serial)
    assert re_search_copy(console, serial)                    # the copy button stays


def re_search_copy(console, serial):
    return f'copytoast:{serial}' in console.console.toHtml()


def test_a_closed_card_is_not_counted_on(main_window, console):
    main_window.show_toast("Saved", level='success')
    QTest.qWait(150)
    serial = dc.get_debug_logger().toasts()[-1][0]
    console.remove_toast(serial)
    main_window.show_toast("Saved", level='success')
    QTest.qWait(150)
    assert _toasts() == [('success', "Saved")]
    assert dc.get_debug_logger().toasts()[-1][0] != serial
    assert "×" not in console.console.toPlainText()


def test_the_count_survives_a_rebuild_and_a_condump(main_window, console):
    for _ in range(4):
        main_window.show_toast("Loaded room_12.json", level='success')
    QTest.qWait(200)
    console._refresh_console()                               # a filter change does this
    serial = dc.get_debug_logger().toasts()[-1][0]
    assert "×4" in _card_text(console, serial)
    entries = dc.get_debug_logger().get_buffer()
    plain = [dc.console_plain_text(c, m) for c, m in entries]
    assert "Loaded room_12.json  ×4" in plain
