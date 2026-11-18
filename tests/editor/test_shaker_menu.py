"""A Shaker (Shake entity) can be placed from a 2D view's right-click menu.

It was registered as an entity but missing from the menu, so there was no way
to put one in a map.
"""

import pytest

pytest.importorskip('PyQt5')

from editor.things import Shake

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('view_name', ['view_top', 'view_front', 'view_side'])
def test_right_click_places_a_shaker(main_window, monkeypatch, view_name):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QContextMenuEvent
    from PyQt5.QtWidgets import QMenu

    offered = []

    def choose_shaker(menu, *args):
        offered.extend(action.text() for action in menu.actions())
        return next(action for action in menu.actions() if action.text() == 'Shaker')

    monkeypatch.setattr(QMenu, 'exec_', choose_shaker)
    main_window.state.clear_scene()
    view = getattr(main_window, view_name)
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(80, 80)))

    assert 'Shaker' in offered
    shaker, = main_window.state.things
    assert type(shaker) is Shake
    assert shaker.properties['type'] == 'shake'
    assert shaker.properties.get('name')
