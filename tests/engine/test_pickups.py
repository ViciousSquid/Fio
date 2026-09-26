"""Regression coverage for pickup weapon representation and collection."""

import pytest

pytest.importorskip("PyQt5", reason="Pickup is an editor Thing")

from editor.things import Pickup, Thing
from engine.logic_thread import LogicThread


pytestmark = pytest.mark.qt


@pytest.mark.parametrize("legacy_weapon", ["gun1", "gun2", "cig"])
def test_legacy_weapon_pickup_is_migrated_without_changing_weapon(legacy_weapon):
    loaded = Thing.from_dict({
        "type": "pickup",
        "pos": [1, 2, 3],
        "properties": {
            "item_type": legacy_weapon,
            "name": "weapon_pickup",
        },
    })

    assert isinstance(loaded, Pickup)
    assert loaded.properties["item_type"] == "weapon"
    assert loaded.properties["weapon"] == legacy_weapon
    assert loaded.get_weapon() == legacy_weapon
    assert loaded.is_gun()


def test_new_weapon_pickup_defaults_to_gun1():
    pickup = Pickup()
    assert pickup.properties["item_type"] == "health"
    assert pickup.properties["weapon"] == "gun1"


def test_weapon_pickup_serializes_explicit_weapon():
    pickup = Pickup(properties={"item_type": "weapon", "weapon": "cig"})
    data = pickup.to_dict()

    assert data["properties"]["item_type"] == "weapon"
    assert data["properties"]["weapon"] == "cig"


def test_collect_pickup_equips_explicit_weapon():
    pickup = Pickup(properties={
        "item_type": "weapon",
        "weapon": "gun2",
        "value": 25,
    })

    logic = object.__new__(LogicThread)
    logic.player_health = 100
    logic.player_max_health = 100
    logic.collected_pickups = set()
    logic.current_hud_message = ""
    logic.io_manager = None
    logic.respawn_timers = {}

    logic._collect_pickup(pickup)

    assert logic.active_weapon == "gun2"
    assert pickup.properties["collected"] is True


def test_collect_legacy_cig_pickup_remains_non_firing_weapon():
    pickup = Pickup(properties={
        "item_type": "cig",
    })

    logic = object.__new__(LogicThread)
    logic.player_health = 100
    logic.player_max_health = 100
    logic.collected_pickups = set()
    logic.current_hud_message = ""
    logic.io_manager = None
    logic.respawn_timers = {}

    logic._collect_pickup(pickup)

    assert logic.active_weapon == "cig"
