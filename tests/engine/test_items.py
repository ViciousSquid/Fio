"""Item definitions: the authoring form, its compilation, and its defaults.

Four stable item slots -- gun1, gun2, custom1, custom2 -- each described by
data. These tests pin the shipped values (gun1/gun2 must play exactly as the
hard-coded pistol and shotgun did), what makes a definition invalid, and that
a broken or unknown item resolves to nothing rather than to some other item.
"""

import copy
import math

import pytest

from engine.items import (
    CUSTOM_ITEM_IDS, DEFAULT_DEFINITIONS, DEFAULT_REGISTRY, ITEM_IDS,
    ItemDefinitionError, ItemDefinitions, ItemRegistry, compile_definition,
    complete_definition,
)


def _custom(item_id, **changes):
    definition = copy.deepcopy(DEFAULT_DEFINITIONS[item_id])
    for key, value in changes.items():
        if isinstance(value, dict):
            definition[key] = {**definition.get(key, {}), **value}
        else:
            definition[key] = value
    return definition


# -- defaults ----------------------------------------------------------------

def test_the_four_slots_are_stable_and_ordered():
    assert ITEM_IDS == ("gun1", "gun2", "custom1", "custom2")
    assert CUSTOM_ITEM_IDS == ("custom1", "custom2")


def test_shipped_names():
    assert [DEFAULT_REGISTRY.resolve(i).name for i in ITEM_IDS] == [
        "Pistol", "Shotgun", "Cigarette", "Wine Glass"]


def test_the_pistol_keeps_its_gameplay():
    w = DEFAULT_REGISTRY.resolve("gun1").weapon
    assert (w.mode, w.damage, w.cooldown, w.ammo, w.ammo_per_shot) == (
        "hitscan", 25.0, 0.0, 0, 0)
    assert w.sound == "shoot.wav" and w.noise == 1.0 and w.pellets == 1


def test_the_shotgun_keeps_its_gameplay():
    w = DEFAULT_REGISTRY.resolve("gun2").weapon
    assert (w.mode, w.damage, w.cooldown, w.ammo, w.ammo_per_shot) == (
        "hitscan", 75.0, 1.0, 8, 1)
    assert w.sound == "shoot2.wav"


@pytest.mark.parametrize("item_id", CUSTOM_ITEM_IDS)
def test_custom_slots_ship_as_held_items_that_never_fire(item_id):
    item = DEFAULT_REGISTRY.resolve(item_id)
    assert item.kind == "weapon"
    assert item.weapon.mode == "none" and not item.weapon.fires
    assert item.weapon.damage == 0 and item.weapon.sound == ""


def test_shipped_sprites_and_hud_placement():
    by_id = {i: DEFAULT_REGISTRY.resolve(i) for i in ITEM_IDS}
    for item_id, item in by_id.items():
        assert item.world_sprite == f"assets/sprites/{item_id}.png"
        assert item.hud_sprite == f"assets/sprites/{item_id}HUD.png"
    assert [by_id[i].hud_align for i in ITEM_IDS] == ["right", "center", "right", "left"]
    assert by_id["custom2"].hud_height == 220.0


def test_shipped_definitions_compile_without_errors():
    assert DEFAULT_REGISTRY.errors == {}
    assert [item.id for item in DEFAULT_REGISTRY] == list(ITEM_IDS)


# -- configuration -----------------------------------------------------------

def test_a_custom_slot_can_be_a_projectile_weapon():
    item = compile_definition("custom1", _custom(
        "custom1", name="Ember", weapon={"mode": "projectile", "damage": 40.0,
                                          "projectile_speed": 600.0, "ammo": 5,
                                          "ammo_per_shot": 1}))
    assert item.name == "Ember"
    assert item.weapon.mode == "projectile"
    assert item.weapon.projectile_speed == 600.0
    assert item.pickup is None


def test_a_custom_slot_can_be_a_pickup():
    item = compile_definition("custom2", _custom(
        "custom2", kind="pickup", pickup={"effect": "armor", "amount": 50}))
    assert item.kind == "pickup"
    assert item.weapon is None
    assert (item.pickup.effect, item.pickup.amount) == ("armor", 50)
    assert item.pickup.activation == "walk_over"


def test_missing_settings_take_unarmed_defaults_not_another_items():
    item = compile_definition("custom1", {"name": "Bare", "weapon": {"mode": "hitscan"}})
    assert item.weapon.damage == 0.0
    assert item.weapon.sound == ""
    assert item.world_sprite == ""


@pytest.mark.parametrize("definition, message", [
    ({"name": ""}, "name"),
    ({"name": "X", "colour": "red"}, "unknown settings"),
    ({"name": "X", "kind": "spell"}, "kind"),
    ({"name": "X", "weapon": {"mode": "laser"}}, "weapon.mode"),
    ({"name": "X", "weapon": {"damage": -1}}, "weapon.damage"),
    ({"name": "X", "weapon": {"damage": math.inf}}, "finite"),
    ({"name": "X", "weapon": {"damage": math.nan}}, "finite"),
    ({"name": "X", "weapon": {"damage": True}}, "number"),
    ({"name": "X", "weapon": {"pellets": 0}}, "weapon.pellets"),
    ({"name": "X", "weapon": {"pellets": 1.5}}, "whole number"),
    ({"name": "X", "weapon": {"ammo_type": "shells"}}, "unknown settings"),
    ({"name": "X", "hud_align": "up"}, "hud_align"),
    ({"name": "X", "kind": "pickup", "pickup": {"effect": "weapon", "item_id": "bfg"}},
     "pickup.item_id"),
    ({"name": "X", "kind": "pickup", "pickup": {"effect": "key", "key_name": ""}},
     "key_name"),
    ({"name": "X", "kind": "pickup", "pickup": {"respawns": 1}}, "true or false"),
])
def test_invalid_definitions_are_rejected_with_a_reason(definition, message):
    with pytest.raises(ItemDefinitionError, match=message):
        compile_definition("custom1", definition)


def test_an_unknown_item_id_is_rejected():
    with pytest.raises(ItemDefinitionError, match="unknown item id"):
        compile_definition("gun3", DEFAULT_DEFINITIONS["gun1"])


def test_an_invalid_definition_resolves_to_nothing_and_never_to_the_pistol():
    definitions = dict(DEFAULT_DEFINITIONS)
    definitions["custom1"] = {"name": "Broken", "weapon": {"mode": "laser"}}
    registry = ItemRegistry.compile(definitions)
    assert registry.resolve("custom1") is None
    assert "weapon.mode" in registry.errors["custom1"]
    assert registry.resolve("gun1").name == "Pistol"
    assert registry.resolve("nonsense") is None
    assert registry.resolve(None) is None


def test_compiled_items_are_immutable():
    item = DEFAULT_REGISTRY.resolve("gun1")
    with pytest.raises(AttributeError):
        item.name = "Other"
    with pytest.raises(AttributeError):
        item.weapon.damage = 1000


def test_damage_is_whole_hit_points():
    with pytest.raises(ItemDefinitionError, match="whole number"):
        compile_definition("custom1", {"name": "X", "weapon": {"damage": 2.5}})
    assert compile_definition("custom1", {"name": "X", "weapon": {"damage": 3.0}}).weapon.damage == 3


@pytest.mark.parametrize("item_id", ITEM_IDS)
def test_the_complete_definition_of_a_shipped_item_is_its_definition(item_id):
    assert complete_definition(DEFAULT_REGISTRY.resolve(item_id)) == DEFAULT_DEFINITIONS[item_id]


def test_the_complete_definition_fills_in_what_was_left_out():
    item = compile_definition("custom1", {"name": "Bare", "kind": "pickup"})
    complete = complete_definition(item)
    assert complete["pickup"]["effect"] == "health"
    assert complete["weapon"]["mode"] == "none"
    assert compile_definition("custom1", complete) == item


# -- the authoring container --------------------------------------------------

def test_definitions_start_at_the_shipped_values_and_save_nothing():
    definitions = ItemDefinitions()
    assert definitions.to_level_data() == {}
    assert definitions.name("custom1") == "Cigarette"
    assert definitions.registry().resolve("custom2").name == "Wine Glass"


def test_only_custom_slots_can_be_redefined():
    definitions = ItemDefinitions()
    with pytest.raises(ItemDefinitionError, match="not a custom item slot"):
        definitions.set_custom("gun1", _custom("gun1", name="Blaster"))


def test_set_custom_validates_and_leaves_nothing_changed_on_error():
    definitions = ItemDefinitions()
    version = definitions.version
    with pytest.raises(ItemDefinitionError):
        definitions.set_custom("custom1", {"name": ""})
    assert definitions.version == version
    assert definitions.name("custom1") == "Cigarette"


def test_a_changed_slot_round_trips_through_level_data():
    definitions = ItemDefinitions()
    definitions.set_custom("custom2", _custom("custom2", name="Goblet",
                                              kind="pickup",
                                              pickup={"effect": "health", "amount": 10}))
    saved = definitions.to_level_data()
    assert set(saved) == {"custom2"}

    loaded = ItemDefinitions()
    loaded.load(saved)
    assert loaded.definition("custom2") == definitions.definition("custom2")
    assert loaded.registry().resolve("custom2").pickup.amount == 10
    assert loaded.name("custom1") == "Cigarette"


def test_load_keeps_an_invalid_definition_as_authored():
    definitions = ItemDefinitions()
    definitions.load({"custom1": {"name": "Broken", "weapon": {"mode": "laser"}}})
    assert definitions.definition("custom1")["name"] == "Broken"
    assert definitions.registry().resolve("custom1") is None
    assert "custom1" in definitions.registry().errors


def test_the_registry_is_compiled_once_per_change():
    definitions = ItemDefinitions()
    first = definitions.registry()
    assert definitions.registry() is first
    definitions.set_custom("custom1", _custom("custom1", name="Cigar"))
    second = definitions.registry()
    assert second is not first
    assert second.resolve("custom1").name == "Cigar"


def test_definition_returns_a_copy():
    definitions = ItemDefinitions()
    copy_ = definitions.definition("custom1")
    copy_["name"] = "Changed"
    assert definitions.name("custom1") == "Cigarette"
