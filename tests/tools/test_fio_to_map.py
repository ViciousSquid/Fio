"""Regression coverage for pickup weapon export."""

from tools.fio_to_map import convert_fio_entity


def _pickup(item_type, weapon=None):
    props = {"type": "Pickup", "item_type": item_type}
    if weapon is not None:
        props["weapon"] = weapon
    return convert_fio_entity({
        "type": "Pickup",
        "pos": [0, 0, 0],
        "properties": props,
    })


def test_new_weapon_pickups_export_to_the_selected_quake_weapon():
    assert _pickup("weapon", "gun1").classname == "weapon_shotgun"
    assert _pickup("weapon", "gun2").classname == "weapon_nailgun"
    assert _pickup("weapon", "cig").classname == "weapon_rocketlauncher"


def test_legacy_weapon_pickups_still_export():
    assert _pickup("gun1").classname == "weapon_shotgun"
    assert _pickup("gun2").classname == "weapon_nailgun"
    assert _pickup("cig").classname == "weapon_rocketlauncher"
