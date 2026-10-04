"""Colored key collection: sprite identity, inventory, doors, and Prop I/O."""

import pytest
from types import SimpleNamespace

pytest.importorskip("PyQt5", reason="logic tests require editor Thing definitions")

from editor.io_handlers import register_all_input_handlers
from editor.io_system import IOManager, OutputConnection
from editor.things import Prop
from engine.prop_runtime import PropSession


pytestmark = pytest.mark.qt


class Logic:
    def __init__(self, things):
        self.editor_state = SimpleNamespace(things=list(things), brushes=[])
        self.io_manager = None
        self.player_runtime = type("PlayerRuntime", (), {"collected_keys": set()})()
        self.interaction_runtime = SimpleNamespace(
            current_hud_message="",
            current_hud_key_name=None,
        )
        self.player_health = 100
        self.player_max_health = 100
        self.session_runtime = SimpleNamespace(physics_world=None, spatial_grid=None)
        self._plugin_emit = lambda *args, **kwargs: None


@pytest.mark.parametrize(
    ("key_name", "sprite"),
    [
        ("blue_key", "assets/sprites/bluekey.png"),
        ("red_key", "assets/sprites/redkey.png"),
        ("yellow_key", "assets/sprites/yellowkey.png"),
    ],
)
def test_key_prop_uses_the_selected_sprite(key_name, sprite):
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "key",
        "collect_key_name": key_name,
    })
    assert prop.get_collect_sprite_path() == sprite
    assert prop.get_sprite_path() == sprite
    assert Prop.get_key_sprite_path(key_name) == sprite


def test_key_prop_defaults_to_blue():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "key",
    })

    assert prop.properties["collect_key_name"] == "blue_key"
    assert prop.get_sprite_path() == "assets/sprites/bluekey.png"


@pytest.mark.parametrize("key_name", ["red_key", "yellow_key"])
def test_colored_key_collects_the_key_and_fires_on_collected_to_a_door(key_name):
    manager = IOManager()
    register_all_input_handlers(manager)

    prop = Prop(
        properties={
            "name": f"{key_name}_prop",
            "collect_enabled": True,
            "collect_type": "key",
            "collect_key_name": key_name,
        }
    )
    door = {
        "name": f"{key_name}_door",
        "id": f"{key_name}-door-id",
        "is_door": True,
        "door_locked": True,
        "_io_connections": [],
    }
    prop.properties["_io_connections"] = [
        OutputConnection(
            output_name="OnCollected",
            target_name=door["name"],
            input_name="Unlock",
            target_id=door["id"],
        )
    ]

    logic = Logic([prop])
    logic.io_manager = manager
    manager.set_logic_thread(logic)
    manager.set_entity_finder(lambda name: door if name == door["name"] else None)
    manager.set_entity_finder_by_id(
        lambda entity_id: door if entity_id == door["id"] else None
    )

    session = PropSession(logic)
    logic.prop_runtime = session
    session.start()
    assert session.collect_prop(prop) is True

    assert key_name in logic.player_runtime.collected_keys
    assert prop.properties["collect_collected"] is True
    assert id(prop) in session.collected_ids
    assert door["door_locked"] is False
