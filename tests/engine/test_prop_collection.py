"""Regression coverage for unified Prop collection."""

import pytest

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")

from editor.things import Prop
from engine.prop_runtime import PropSession


pytestmark = pytest.mark.qt


def test_prop_collection_defaults_are_independent_of_carry():
    prop = Prop()
    assert prop.properties["collect_enabled"] is False
    assert prop.properties["collect_type"] == "weapon"
    assert prop.properties["collect_weapon"] == "gun1"
    assert prop.properties["carry_enabled"] is False


def test_weapon_prop_serializes_explicit_collection_data():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "weapon",
        "collect_weapon": "cig",
    })
    data = prop.to_dict()

    assert data["type"] == "prop"
    assert data["properties"]["collect_type"] == "weapon"
    assert data["properties"]["collect_weapon"] == "cig"


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
    logic.player_ammo = 2
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.player_ammo == 10


def test_first_gun2_collection_gives_eight_ammo():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "weapon",
        "collect_weapon": "gun2",
    })
    logic = _logic_for(prop)
    logic.player_ammo = 0
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.active_weapon == "gun2"
    assert logic.player_ammo == 8
    assert logic.gun2_obtained is True


def test_later_gun2_collection_does_not_reset_existing_ammo():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "weapon",
        "collect_weapon": "gun2",
    })
    logic = _logic_for(prop)
    logic.player_ammo = 3
    logic.gun2_obtained = True
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.active_weapon == "gun2"
    assert logic.player_ammo == 3


def _logic_for(prop):
    return type(
        "CollectionLogic",
        (),
        {
            "editor_state": type("EditorState", (), {"things": [prop], "brushes": []})(),
            "player_health": 100,
            "player_max_health": 100,
            "player_ammo": 0,
            "active_weapon": "gun1",
            "gun2_obtained": False,
            "collected_keys": set(),
            "current_hud_message": "",
            "current_hud_key_name": None,
            "io_manager": None,
            "_physics_world": None,
            "_plugin_emit": lambda self, *args, **kwargs: None,
        },
    )()


def test_collect_prop_equips_explicit_weapon():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "weapon",
        "collect_weapon": "gun2",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.active_weapon == "gun2"
    assert prop.properties["collect_collected"] is True
    assert id(prop) in session.collected_ids


def test_collect_prop_equips_cig_weapon():
    prop = Prop(properties={
        "collect_enabled": True,
        "collect_type": "weapon",
        "collect_weapon": "cig",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    session.collect_prop(prop)
    assert logic.active_weapon == "cig"


def test_cigarette_is_a_non_firing_weapon():
    from engine.monster_constants import NON_FIRING_WEAPONS

    assert "cig" in NON_FIRING_WEAPONS
    assert "sword" not in NON_FIRING_WEAPONS


# ---------------------------------------------------------------------------
# There is no "custom" collection type
# ---------------------------------------------------------------------------

def test_there_is_no_custom_collection_type():
    assert Prop.COLLECT_TYPES == ("health", "ammo", "weapon", "key")


def test_the_showcase_shotgun_is_a_gun_again():
    """_SHOWCASE's gun2 pickup came out of the Pickup-to-Prop migration as
    collect_type "custom" with the gun's sprite. Walking over it made it
    vanish and gave the player nothing. As authored then, it must now hand
    over the gun."""
    prop = Prop(properties={
        "name": "Pickup_1", "collect_enabled": True, "collect_type": "custom",
        "collect_weapon": "gun2", "collect_activation": "walk_over",
        "sprite_path": "assets/sprites/gun2.png", "collect_custom_sprite": "",
    })
    logic = _logic_for(prop)
    session = PropSession(logic)
    session.start()

    assert session.collect_prop(prop) is True
    assert logic.active_weapon == "gun2"
    assert logic.gun2_obtained is True
    assert logic.player_ammo >= 8


@pytest.mark.parametrize("sprite, kind, field, value", [
    ("assets/sprites/gun1.png", "weapon", "collect_weapon", "gun1"),
    ("assets/sprites/cig.png", "weapon", "collect_weapon", "cig"),
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
