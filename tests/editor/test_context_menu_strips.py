"""The 2D views' right-click menu: an orange strip along the top of the menu,
a green one along the top of every submenu (Fio's two colours)."""

import pytest

pytest.importorskip("PyQt5")

pytestmark = pytest.mark.qt

ORANGE = (0xFF, 0x8C, 0x00)
GREEN = (0x4C, 0xAF, 0x50)


def _top_strip(menu):
    """The colour of the menu's top edge, from its rendered pixels."""
    from PyQt5.QtGui import QColor
    menu.adjustSize()
    image = menu.grab().toImage()
    colour = QColor(image.pixel(image.width() // 2, 1))
    return colour.red(), colour.green(), colour.blue()


def test_the_menu_has_an_orange_strip_and_its_submenus_green(qt_app):
    from PyQt5.QtWidgets import QMenu, QWidget
    from editor.view_2d import CONTEXT_MENU_STYLE
    host = QWidget()
    menu = QMenu(host)
    menu.setStyleSheet(CONTEXT_MENU_STYLE)
    menu.addAction("Light")
    sub = menu.addMenu("Logic Entities")
    sub.addAction("Relay")
    deeper = sub.addMenu("More")
    deeper.addAction("Timer")
    try:
        assert _top_strip(menu) == ORANGE
        assert _top_strip(sub) == GREEN
        assert _top_strip(deeper) == GREEN
    finally:
        host.deleteLater()


def test_the_2d_view_uses_that_style():
    import inspect
    from editor import view_2d
    source = inspect.getsource(view_2d.View2D.contextMenuEvent)
    assert "setStyleSheet(CONTEXT_MENU_STYLE)" in source
