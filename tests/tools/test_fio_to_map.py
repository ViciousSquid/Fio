"""Regression coverage for prop weapon export."""

from tools.fio_to_map import convert_fio_entity


def _prop(collect_type, weapon=None):
    props = {"type": "prop", "collect_enabled": True, "collect_type": collect_type}
    if weapon is not None:
        props["collect_weapon"] = weapon
    return convert_fio_entity({
        "type": "prop",
        "pos": [0, 0, 0],
        "properties": props,
    })


def test_new_weapon_props_export_to_the_selected_quake_weapon():
    assert _prop("weapon", "gun1").classname == "weapon_shotgun"
    assert _prop("weapon", "gun2").classname == "weapon_nailgun"
    assert _prop("weapon", "custom1").classname == "weapon_rocketlauncher"
    assert _prop("weapon", "custom2").classname == "weapon_rocketlauncher"


def test_legacy_weapon_props_still_export():
    assert _prop("gun1").classname == "weapon_shotgun"
    assert _prop("gun2").classname == "weapon_nailgun"
    assert _prop("custom1").classname == "weapon_rocketlauncher"
