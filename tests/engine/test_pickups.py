"""Regression coverage for unified Prop collection and legacy Pickup migration."""

import pytest

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")

from editor.things import Prop, Thing
from engine.prop_runtime import PropSession


pytestmark = pytest.mark.qt


@pytest.mark.parametrize("legacy_weapon", ["gun1", "gun2", "cig"])
def test_legacy_pickup_map_migrates_to_collectible_prop(legacy_weapon):
    loaded = Thing.from_dict({
        "type": "pickup",
        "pos": [1, 2, 3],
        "properties": {
            "item_type": legacy_weapon,
            "name": "weapon_prop",
        },
    })

    assert isinstance(loaded, Prop)
    assert loaded.properties["type"] == "prop"
    assert loaded.properties["collect_enabled"] is True
    assert loaded.properties["carry_enabled"] is False
    assert loaded.properties["collect_type"] == "weapon"
    assert loaded.properties["collect_weapon"] == legacy_weapon
    assert loaded.properties["sprite_path"].endswith(f"{legacy_weapon}.png")


def test_prop_collection_defaults_are_independent_of_carry():
    prop = Prop()
    assert prop.properties["collect_enabled"] is False
    assert prop.properties["collect_type"] == "health"
    assert prop.properties["collect_weapon"] == "gun1"
    assert prop.properties["carry_enabled"] is True


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


def _logic_for(prop):
    return type(
        "CollectionLogic",
        (),
        {
            "things": [prop],
            "player_health": 100,
            "player_max_health": 100,
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


def test_collect_prop_equips_legacy_cig_weapon():
    loaded = Thing.from_dict({
        "type": "pickup",
        "properties": {"item_type": "cig"},
    })
    logic = _logic_for(loaded)
    session = PropSession(logic)
    session.start()

    session.collect_prop(loaded)
    assert logic.active_weapon == "cig"
