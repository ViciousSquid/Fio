"""Colored key collection through the real PropSession and LogicThread."""

import pytest

pytest.importorskip("PyQt5", reason="logic tests require editor Thing definitions")

from editor.editor_state import EditorState
from editor.io_system import OutputConnection
from editor.things import Prop
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt


@pytest.fixture
def logic(request):
    value = LogicThread(ThreadedGameState(), EditorState())
    request.addfinalizer(value.stop)
    return value


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
def test_colored_key_collects_the_key_and_fires_on_collected_to_a_door(
    logic, key_name
):
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

    logic.editor_state.things[:] = [prop]
    logic.editor_state.brushes[:] = [door]
    logic.world_runtime.build_entity_caches()

    session = logic.prop_runtime
    session.start()
    try:
        assert session.collect_prop(prop) is True
        assert key_name in logic.player_runtime.collected_keys
        assert prop.properties["collect_collected"] is True
        assert id(prop) in session.collected_ids
        assert door["door_locked"] is False
    finally:
        session.stop()
