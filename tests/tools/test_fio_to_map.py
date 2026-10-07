"""Regression coverage for collectible item export."""

from engine.items import DEFAULT_DEFINITIONS, ItemDefinitions
from tools.fio_to_map import NO_QUAKE_EQUIVALENT, FioToMapConverter, convert_fio_entity


def _prop(collect_type, item=None, field="collect_item", definitions=None):
    props = {"type": "prop", "collect_enabled": True, "collect_type": collect_type}
    if item is not None:
        props[field] = item
    return convert_fio_entity({
        "type": "prop",
        "pos": [0, 0, 0],
        "properties": props,
    }, definitions)


def _custom(slot, **pickup):
    definitions = ItemDefinitions()
    definition = definitions.definition(slot)
    definition["kind"] = "pickup"
    definition["pickup"] = pickup
    definitions.set_custom(slot, definition)
    return definitions


def test_built_in_weapons_export_to_their_quake_weapons():
    assert _prop("item", "gun1").classname == "weapon_shotgun"
    assert _prop("item", "gun2").classname == "weapon_nailgun"


def test_default_custom_items_have_no_quake_equivalent():
    """A held custom item is not some Quake weapon in disguise."""
    assert _prop("item", "custom1").classname == NO_QUAKE_EQUIVALENT
    assert _prop("item", "custom2").classname == NO_QUAKE_EQUIVALENT


def test_a_custom_pickup_exports_as_what_it_gives():
    assert _prop("item", "custom1", definitions=_custom(
        "custom1", effect="armor", amount=50)).classname == "item_armor1"
    assert _prop("item", "custom2", definitions=_custom(
        "custom2", effect="health", amount=10)).classname == "item_health"
    assert _prop("item", "custom1", definitions=_custom(
        "custom1", effect="weapon", item_id="gun2")).classname == "weapon_nailgun"


def test_maps_written_before_items_were_data_still_export():
    assert _prop("weapon", "gun1", field="collect_weapon").classname == "weapon_shotgun"
    assert _prop("weapon", "gun2", field="collect_weapon").classname == "weapon_nailgun"
    assert _prop("gun1").classname == "weapon_shotgun"
    assert _prop("gun2").classname == "weapon_nailgun"
    assert _prop("custom1").classname == NO_QUAKE_EQUIVALENT


def test_the_converter_reads_the_maps_own_item_definitions():
    definition = dict(DEFAULT_DEFINITIONS["custom2"], kind="pickup",
                      pickup={"effect": "ammo", "amount": 4})
    level = {
        "brushes": [],
        "items": {"custom2": definition},
        "things": [{"type": "prop", "pos": [0, 0, 0], "properties": {
            "type": "prop", "collect_enabled": True,
            "collect_type": "item", "collect_item": "custom2"}}],
    }
    text = FioToMapConverter().convert(level)
    assert '"classname" "item_rockets"' in text
