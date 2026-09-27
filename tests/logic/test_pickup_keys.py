"""Colored pickup keys: sprite identity, inventory identity, doors, and I/O."""

import pytest

from editor.io_handlers import register_all_input_handlers
from editor.io_system import IOManager, OutputConnection
from editor.things import Pickup
from engine.logic_thread import LogicThread


pytestmark = pytest.mark.qt


@pytest.mark.parametrize(
    ("key_name", "sprite"),
    [
        ("blue_key", "assets/sprites/bluekey.png"),
        ("red_key", "assets/sprites/redkey.png"),
        ("yellow_key", "assets/sprites/yellowkey.png"),
    ],
)
def test_key_pickup_uses_the_selected_sprite(key_name, sprite):
    pickup = Pickup(
        properties={
            "item_type": "key",
            "key_name": key_name,
        }
    )

    assert pickup.get_key_name() == key_name
    assert pickup.get_sprite_path() == sprite
    assert Pickup.get_key_sprite_path(key_name) == sprite


def test_key_pickup_defaults_to_blue():
    pickup = Pickup(properties={"item_type": "key"})

    assert pickup.get_key_name() == "blue_key"
    assert pickup.get_sprite_path() == "assets/sprites/bluekey.png"


@pytest.mark.parametrize("key_name", ["red_key", "yellow_key"])
def test_colored_key_pickup_collects_the_key_and_fires_on_picked_up_to_a_door(
        key_name):
    manager = IOManager()
    register_all_input_handlers(manager)

    pickup = Pickup(
        properties={
            "name": "%s_pickup" % key_name,
            "item_type": "key",
            "key_name": key_name,
            "collected": False,
            "respawns": False,
        }
    )
    door = {
        "name": "%s_door" % key_name,
        "id": "%s-door-id" % key_name,
        "is_door": True,
        "door_locked": True,
        "_io_connections": [],
    }
    pickup.properties["_io_connections"] = [
        OutputConnection(
            output_name="OnPickedUp",
            target_name=door["name"],
            input_name="Unlock",
            target_id=door["id"],
        )
    ]

    logic = type(
        "PickupLogic",
        (),
        {
            "io_manager": manager,
            "collected_keys": set(),
            "collected_pickups": set(),
            "respawn_timers": {},
            "_plugin_emit": lambda self, *args, **kwargs: None,
        },
    )()
    manager.set_logic_thread(logic)
    manager.set_entity_finder(lambda name: door if name == door["name"] else None)
    manager.set_entity_finder_by_id(
        lambda entity_id: door if entity_id == door["id"] else None
    )

    LogicThread._collect_pickup(logic, pickup)

    assert key_name in logic.collected_keys
    assert pickup.properties["collected"] is True
    assert id(pickup) in logic.collected_pickups
    assert door["door_locked"] is False
