"""Regression coverage for unified Prop collection."""

import pytest

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")

from editor.things import Prop
from engine.logic_combat import LogicCombat
from engine.prop_runtime import PropSession


pytestmark = pytest.mark.qt


def test_prop_collection_defaults_are_independent_of_carry():
    prop = Prop()
    assert prop.properties["collect_enabled"] is False
    assert prop.properties["collect_type"] == "item"
    assert prop.properties["collect_item"] == "gun1"
    assert prop.properties["carry_enabled"] is False


def test_item_prop_serializes_explicit_collection_data():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "item",
        "collect_item": "custom1",
    })
    data = prop.to_dict()

    assert data["type"] == "prop"
    assert data["properties"]["collect_type"] == "item"
    assert data["properties"]["collect_item"] == "custom1"
    assert "collect_weapon" not in data["properties"]


def test_ammo_collectible_uses_stock_sprite_and_defaults_to_eight():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "ammo",
    })

    assert prop.properties["collect_value"] == 8
    assert prop.properties["sprite_path"] == "assets/sprites/ammo.png"


def test_collect_ammo_awards_eight():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "ammo",
    })
    logic = _logic_for(prop)
    logic.combat_runtime.player_ammo = 2
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.combat_runtime.player_ammo == 10


def test_first_gun2_collection_gives_eight_ammo():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "item",
        "collect_item": "gun2",
    })
    logic = _logic_for(prop)
    logic.combat_runtime.player_ammo = 0
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.combat_runtime.active_weapon == "gun2"
    assert logic.combat_runtime.player_ammo == 8
    assert logic.combat_runtime.weapons == {"gun2"}


def test_later_gun2_collection_does_not_reset_existing_ammo():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "item",
        "collect_item": "gun2",
    })
    logic = _logic_for(prop)
    logic.combat_runtime.player_ammo = 3
    logic.combat_runtime.weapons = {"gun2"}
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.combat_runtime.active_weapon == "gun2"
    assert logic.combat_runtime.player_ammo == 3


def _logic_for(prop):
    logic = type(
        "CollectionLogic",
        (),
        {
            "editor_state": type("EditorState", (), {"things": [prop], "brushes": []})(),
            "player_runtime": type("PlayerRuntime", (), {
                "collected_keys": set(),
                "player_health": 100,
                "player_max_health": 100,
                "player_dead": False,
                "player2_health": 100,
                "player2_max_health": 100,
                "player2_dead": False,
            })(),
            "interaction_runtime": type("InteractionRuntime", (), {
                "current_hud_message": "",
                "current_hud_key_name": None,
            })(),
            "io_manager": None,
            "session_runtime": type("SessionRuntime", (), {
                "physics_world": None,
                "spatial_grid": None,
            })(),
            "_plugin_emit": lambda self, *args, **kwargs: None,
        },
    )()
    logic.combat_runtime = LogicCombat(logic)
    return logic


def test_collect_prop_equips_explicit_weapon():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "item",
        "collect_item": "gun2",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.combat_runtime.active_weapon == "gun2"
    assert prop.properties["collect_collected"] is True
    assert id(prop) in session.collected_ids


def test_collect_prop_equips_custom1_weapon():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "item",
        "collect_item": "custom1",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    session.collect_prop(prop)
    assert logic.combat_runtime.active_weapon == "custom1"


def test_default_custom_weapons_are_non_firing():
    from engine.items import DEFAULT_REGISTRY

    assert DEFAULT_REGISTRY.resolve("custom1").weapon.fires is False
    assert DEFAULT_REGISTRY.resolve("custom2").weapon.fires is False
    assert DEFAULT_REGISTRY.resolve("gun1").weapon.fires is True


# ---------------------------------------------------------------------------
# There is no "custom" collection type
# ---------------------------------------------------------------------------

def test_there_is_no_custom_collection_type():
    assert Prop.COLLECT_TYPES == ("health", "ammo", "item", "key")


def test_the_showcase_shotgun_is_a_gun_again():
    """_SHOWCASE's gun2 pickup came out of the Pickup-to-Prop migration as
    collect_type "custom" with the gun's sprite. Walking over it made it
    vanish and gave the player nothing. As authored then, it must now hand
    over the gun."""
    prop = Prop(properties={
        "name": "Pickup_1", "collect_enabled": True, "collect_type": "custom",
        "collect_item": "gun2", "collect_activation": "walk_over",
        "sprite_path": "assets/sprites/gun2.png", "collect_custom_sprite": "",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.combat_runtime.active_weapon == "gun2"
    assert "gun2" in logic.combat_runtime.weapons
    assert logic.combat_runtime.player_ammo >= 8


@pytest.mark.parametrize("sprite, kind, field, value", [
    ("assets/sprites/gun1.png", "item", "collect_item", "gun1"),
    ("assets/sprites/custom1.png", "item", "collect_item", "custom1"),
    ("assets/sprites/custom2.png", "item", "collect_item", "custom2"),
    ("assets/sprites/redkey.png", "key", "collect_key_name", "red_key"),
    ("assets/sprites/ammo.png", "ammo", None, None),
    ("assets/sprites/pickup.png", "health", None, None),
    ("assets/sprites/some_trophy.png", "health", None, None),
])
def test_an_old_custom_pickup_is_read_from_its_sprite(sprite, kind, field, value):
    prop = Prop(properties={"collect_enabled": True, "collect_type": "custom",
                            "sprite_path": sprite})
    assert prop.properties["collect_type"] == kind
    if field:
        assert prop.properties[field] == value


def test_a_health_pickup_keeps_its_own_sprite():
    prop = Prop(properties={"collect_enabled": True, "collect_type": "health",
                            "collect_custom_sprite": "assets/sprites/medkit.png"})
    assert prop.properties["collect_type"] == "health"
    assert prop.get_collect_sprite_path() == "assets/sprites/medkit.png"


def test_no_shipped_map_has_a_custom_pickup():
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "maps"
    offenders = []
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        things = data.get("things", []) if isinstance(data, dict) else []
        for thing in things:
            props = thing.get("properties", {}) if isinstance(thing, dict) else {}
            if props.get("collect_type", "health") not in Prop.COLLECT_TYPES:
                offenders.append("%s: %s" % (path.name, props.get("name")))
    assert not offenders, offenders


@pytest.mark.parametrize("item_id", ["gun1", "gun2", "custom1", "custom2"])
def test_a_weapon_prop_from_before_items_were_data_keeps_its_weapon(item_id):
    """``collect_weapon`` is the item id; the collect_item default filled in
    beside it must not win."""
    prop = Prop(properties={"collect_enabled": True, "collect_type": "weapon",
                            "collect_weapon": item_id})
    assert prop.properties["collect_type"] == "item"
    assert prop.properties["collect_item"] == item_id
    assert "collect_weapon" not in prop.properties
