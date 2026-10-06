"""The status-bar notification shows the latest toast for its full duration."""

import pytest

pytest.importorskip("PyQt5", reason="the notification area is editor-tier")

from PyQt5.QtTest import QTest  # noqa: E402

pytestmark = pytest.mark.qt


def test_an_earlier_toast_does_not_clear_a_later_one(main_window):
    """Each toast used to arm its own clear timer, so a short toast followed by
    a long one blanked the long one when the short one's time ran out."""
    label = main_window.ui.notification_label

    main_window.show_toast("first", duration=100)
    main_window.show_toast("second", duration=2000)
    QTest.qWait(300)

    assert label.text() == "SECOND"


def test_a_toast_clears_itself_after_its_duration(main_window):
    label = main_window.ui.notification_label

    main_window.show_toast("short", duration=100)
    QTest.qWait(300)

    assert label.text() == ""


def test_a_zero_duration_tooltip_cancels_a_pending_clear(main_window):
    label = main_window.ui.notification_label

    main_window.show_toast("short", duration=100)
    main_window.show_tooltip("sticky", duration=0)
    QTest.qWait(300)

    assert label.text() == "STICKY"
