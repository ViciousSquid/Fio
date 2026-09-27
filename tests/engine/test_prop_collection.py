"""Regression coverage for unified Prop collection."""

import pytest

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")

from editor.things import Prop
from engine.prop_runtime import PropSession


pytestmark = pytest.mark.qt


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
            "things": [prop],
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
