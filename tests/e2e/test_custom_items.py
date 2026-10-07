"""A map's own custom items, end to end: authored, saved, loaded, played.

Custom 1 is redefined as a hitscan weapon and Custom 2 as an armor pickup in
the map's ``items``. The map goes through the editor's loader; in play the
player walks over both, fires the custom weapon through the input queue, and
switches weapons with the slot keys -- every effect read back from the
published frame or the world, as the renderer and the player would see it.
"""

import copy
import json

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt                           # noqa: E402
from PyQt5.QtGui import QKeyEvent                             # noqa: E402

from editor.things import Monster, PlayerStart, Prop          # noqa: E402
from engine.items import DEFAULT_DEFINITIONS                  # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]


def _items():
    blaster = copy.deepcopy(DEFAULT_DEFINITIONS["custom1"])
    blaster["name"] = "Blaster"
    blaster.update(hud_sprite="assets/sprites/gun2HUD.png", hud_align="center",
                   world_sprite="assets/sprites/gun1.png")
    blaster["weapon"].update(mode="hitscan", damage=40, ammo=2, ammo_per_shot=1,
                             sound="shoot2.wav", noise=1.0,
                             muzzle_flash="assets/sprites/gun2HUD_flash.png")
    vest = copy.deepcopy(DEFAULT_DEFINITIONS["custom2"])
    vest.update(name="Vest", kind="pickup")
    vest["pickup"].update(effect="armor", amount=30)
    return {"custom1": blaster, "custom2": vest}


def _level():
    def pickup(name, item_id):
        return make_thing(Prop, name, (0.0, 40.0, 0.0), collect_enabled=True,
                          collect_type="item", collect_item=item_id)

    things = [
        make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=270.0),  # faces +z
        pickup("blaster", "custom1"),
        pickup("vest", "custom2"),
        make_thing(Monster, "target", (0.0, 0.0, 600.0), health=100),
    ]
    level = level_data(brushes=room(size=2048.0, height=512.0), things=things,
                       items=_items())
    # Through JSON, as a saved map is.
    return json.loads(json.dumps(level))


def _press(session, key):
    session.view.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def _published(session, name):
    session.publish()
    session.game_state.try_swap()
    return session.game_state.published(name)


def test_a_maps_custom_items_play_as_defined(fio_session):
    session = fio_session(_level())
    state = session.state
    assert state.item_definitions.name("custom1") == "Blaster"
    by_name = {t.properties["name"]: t for t in state.things}
    # Each pickup shows its item's world sprite, as the map defines it.
    assert by_name["blaster"].properties["sprite_path"] == "assets/sprites/gun1.png"
    assert by_name["vest"].properties["sprite_path"] == "assets/sprites/custom2.png"

    session.start_play().step(3)

    combat = session.logic.combat_runtime
    assert combat.weapons == {"custom1"}
    assert _published(session, "active_weapon") == "custom1"
    assert _published(session, "player_ammo") == 2
    assert _published(session, "player_armor") == 30

    # The HUD draws what the session compiled for the item in hand.
    held = session.view._session_item(_published(session, "active_weapon"))
    assert (held.hud_sprite, held.hud_align) == ("assets/sprites/gun2HUD.png", "center")
    assert held.weapon.muzzle_flash == "assets/sprites/gun2HUD_flash.png"
    assert session.view._hud_pixmap(held.hud_sprite) is not None
    assert session.view._hud_pixmap(held.world_sprite) is not None

    target = next(t for t in session.logic.editor_state.things
                  if t.properties.get("name") == "target")
    before = int(target.properties["health"])
    session.step(1, shoot=True)
    assert int(target.properties["health"]) == before - 40
    assert _published(session, "player_ammo") == 1

    # Slot 1 (the pistol) is not held: nothing happens.
    serial = _published(session, "weapon_switch_serial")
    _press(session, Qt.Key_1)
    session.step(1)
    assert _published(session, "active_weapon") == "custom1"
    assert _published(session, "weapon_switch_serial") == serial

    # Slot 3 is: the switch is published for the HUD's flash.
    _press(session, Qt.Key_3)
    session.step(1)
    assert _published(session, "weapon_switch_serial") == serial + 1


def test_custom_items_in_play_do_not_leak_into_the_next_map(fio_session):
    session = fio_session(_level())
    session.window._load_level(level_data(brushes=room(), things=[
        make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0))]))
    assert session.state.item_definitions.name("custom1") == "Cigarette"
    assert session.state.item_definitions.to_level_data() == {}
