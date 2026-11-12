"""A Monster placed from a 2D view stands on the clicked height.

``Monster.pos`` is the centre of its billboard (the AI grounds a monster at
``floor + sprite_height / 2`` and the renderer draws the quad centred on
``pos``).  Placement used to put that centre at the clicked height, so the
lower half of a 192-unit human sank through the floor and the soldier looked
shorter than the 100-unit player beside it.
"""

import pytest

pytest.importorskip('PyQt5')

from editor.things import Monster
from engine.entity_table import sprite_size
from engine.monster_constants import MONSTER_SPRITE_SIZES

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('view_name', ['view_top', 'view_front', 'view_side'])
def test_right_click_places_a_monster_with_its_feet_on_the_click(
        main_window, monkeypatch, view_name):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QContextMenuEvent
    from PyQt5.QtWidgets import QMenu

    def choose_monster(menu, *args):
        return next(action for action in menu.actions() if action.text() == 'Monster')

    monkeypatch.setattr(QMenu, 'exec_', choose_monster)
    main_window.state.clear_scene()
    view = getattr(main_window, view_name)
    clicked = view.snap_to_grid(view.screen_to_world(QPoint(80, 80)))
    view.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(80, 80)))

    monster, = main_window.state.things
    assert type(monster) is Monster
    width, height = MONSTER_SPRITE_SIZES['human']
    assert (monster.properties['sprite_width'],
            monster.properties['sprite_height']) == (width, height)
    # What the renderer draws is exactly the authored size ...
    assert sprite_size(monster) == (float(width), float(height))
    # ... and the bottom of that quad is the clicked height, not half below it.
    expected_feet_y = 0 if view_name == 'view_top' else clicked.y()
    assert monster.pos[1] - height / 2.0 == pytest.approx(expected_feet_y)
