"""Regression checks for Qt 5 popup-menu hover artefacts on macOS."""

import pytest

pytest.importorskip("PyQt5", reason="Qt editor is unavailable")

from PyQt5.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QMenu  # noqa: E402

from editor import desktop_style  # noqa: E402

pytestmark = pytest.mark.qt


class TrackingMenu(QMenu):
    def __init__(self):
        super().__init__()
        self.full_update_requests = 0

    def update(self, *args):
        if not args:
            self.full_update_requests += 1
        return super().update(*args)


def test_macos_mouse_move_invalidates_the_whole_popup(qt_app, monkeypatch):
    """Partial hover redraws can leave orange lines at action boundaries."""
    menu = TrackingMenu()
    event = QMouseEvent(
        QEvent.MouseMove, QPoint(5, 5),
        Qt.NoButton, Qt.NoButton, Qt.NoModifier,
    )
    rules = desktop_style.EditorLayoutRules()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(desktop_style.sys, "platform", "darwin")
            assert rules.eventFilter(menu, event) is False
        assert menu.full_update_requests == 1

        # Non-macOS platforms retain Qt's normal (partial) invalidation.
        with monkeypatch.context() as patch:
            patch.setattr(desktop_style.sys, "platform", "linux")
            assert rules.eventFilter(menu, event) is False
        assert menu.full_update_requests == 1
    finally:
        menu.deleteLater()
