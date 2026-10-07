"""The slot-key flash and the collected keys share the corner, in real pixels.

Painted through ``QtGameView.paintGL``: with a key collected, pressing a weapon
slot shows the weapon's sprite in the bottom-right slot, and the key moves
left of it instead of being covered.
"""

import time

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt                           # noqa: E402
from PyQt5.QtGui import QKeyEvent                             # noqa: E402

from editor.things import PlayerStart                        # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]

SIZE = (640, 360)
MARGIN, ICON, GAP = 20, 100, 15


def _region(image, right):
    """The 100x100 icon slot whose right edge is *right* pixels from the edge."""
    h, w, _ = image.shape
    return image[h - MARGIN - ICON:h - MARGIN, w - right - ICON:w - right].astype(np.int16)


def _changed(a, b):
    return float(np.abs(a - b).mean())


def test_the_slot_flash_and_the_keys_share_the_corner(fio_session):
    session = fio_session(level_data(brushes=room(size=2048.0), things=[
        make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=270.0)]), size=SIZE)
    session.view.sysmon.set_active(False)
    session.start_play()
    combat = session.logic.combat_runtime
    # The shotgun in hand (its first-person art is centred, clear of the
    # corner); pressing 2 takes it again, which still flashes it.
    combat.give_weapon(combat.items.resolve("gun1"))
    combat.give_weapon(combat.items.resolve("gun2"))
    session.logic.player_runtime.collected_keys.add("blue_key")
    session.step(2)
    session.paint()
    before = session.paint()

    session.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_2, Qt.NoModifier))
    session.step(1)
    session.paint()                       # the view sees the switch...
    time.sleep(QtGameViewTiming.mid_hold())
    during = session.paint()              # ...and shows it at full strength

    item_slot = MARGIN
    key_after = MARGIN + ICON + GAP
    # The weapon now fills the corner slot the key had...
    assert _changed(_region(before, item_slot), _region(during, item_slot)) > 8.0
    # ...and the key moved left of it, where nothing was drawn before.
    assert _changed(_region(before, key_after), _region(during, key_after)) > 8.0

    time.sleep(1.0)
    after = session.paint()
    # Once the flash is over the key is back in the corner.
    assert _changed(_region(before, item_slot), _region(after, item_slot)) < 2.0


class QtGameViewTiming:
    @staticmethod
    def mid_hold():
        from engine.qt_game_view import QtGameView
        return QtGameView.WEAPON_SWITCH_FADE_IN + QtGameView.WEAPON_SWITCH_HOLD / 2
