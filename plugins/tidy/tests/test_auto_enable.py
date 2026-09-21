"""
Headless tests for Tidy's disabled-by-default activation.

Tidy now activates for its own receptacle/goal types and for core Props carrying
tidy_category. Legacy tidyobject maps are migrated before activation.
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _tidy(mgr):
    for p in mgr.plugins:
        if p.name == "tidy":
            return p
    raise AssertionError("tidy plugin not loaded")


def test_disabled_by_default():
    print("[1] tidy plugin ships disabled")
    from plugins.manager import load_plugins, get_manager
    load_plugins()
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)
    _check(mgr.is_enabled(tidy) is False, "tidy is disabled by default")


def test_plain_and_tidy_maps():
    print("[2] map activation distinguishes ordinary Props from Tidy Props")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    plain = {
        "version": 3,
        "things": [{
            "type": "prop",
            "properties": {"type": "prop", "name": "barrel"},
        }],
    }
    _check(mgr.auto_enable_for_map(plain) == [], "plain Prop map enables nothing")
    _check(mgr.is_enabled(tidy) is False, "tidy stays off for plain Props")

    tidy_map = {
        "version": 3,
        "things": [{
            "type": "prop",
            "properties": {
                "type": "prop",
                "tidy_category": "book",
                "name": "book1",
            },
        }],
    }
    enabled = mgr.auto_enable_for_map(tidy_map)
    _check([p.name for p in enabled] == ["tidy"], "marked Prop map enables tidy")
    _check(mgr.is_enabled(tidy) is True, "tidy is on for marked core Props")


def test_legacy_map_migration_auto_enables():
    print("[3] legacy tidyobject map migrates and auto-enables")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    legacy = {
        "version": 3,
        "things": [{
            "type": "tidyobject",
            "pos": [0, 40, 35],
            "properties": {
                "type": "tidyobject",
                "category": "book",
                "name": "book1",
            },
        }],
    }
    enabled = mgr.auto_enable_for_map(legacy)
    thing = legacy["things"][0]
    _check([p.name for p in enabled] == ["tidy"], "legacy map enables tidy")
    _check(thing["type"] == "prop", "legacy map was migrated to prop")
    _check(thing["properties"]["tidy_category"] == "book",
           "legacy category became tidy_category")


def test_disable_auto_enabled_on_clear():
    print("[4] a cleared/new scene reverts a level-driven auto-enable")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    mgr.auto_enable_for_map({
        "version": 3,
        "things": [{
            "type": "prop",
            "properties": {"type": "prop", "tidy_category": "book"},
        }],
    })
    _check(mgr.is_enabled(tidy) is True, "tidy auto-enabled for the level")
    reverted = mgr.disable_auto_enabled()
    _check([p.name for p in reverted] == ["tidy"], "clear reverts the auto-enable")
    _check(mgr.is_enabled(tidy) is False, "tidy is off after New/empty scene")

    mgr.set_enabled(tidy, True)
    _check(mgr.disable_auto_enabled() == [],
           "manual enable is not treated as auto")
    _check(mgr.is_enabled(tidy) is True, "manual enable survives clear")
    mgr.set_enabled(tidy, False)


def test_player_host_auto_enables():
    print("[5] player host auto-enables tidy from marked core Prop data")
    from plugins.manager import get_manager
    from player.plugin_host import PlayerPluginHost
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    map_data = {
        "version": 3,
        "brushes": [],
        "things": [
            {
                "type": "prop",
                "pos": [0, 40, 35],
                "properties": {
                    "type": "prop",
                    "tidy_category": "book",
                    "name": "b1",
                },
            },
            {
                "type": "tidyreceptacle",
                "pos": [0, 40, -60],
                "properties": {
                    "type": "tidyreceptacle",
                    "accepts": "book",
                    "name": "s",
                },
            },
        ],
    }

    host = PlayerPluginHost()
    _check(host.load(package=None) is True, "player host loaded plugins")
    host.build_and_start(map_data)
    _check(mgr.is_enabled(tidy) is True, "building a Tidy map auto-enabled the plugin")
    _check(host.active is True and host.bridge is not None,
           "play session started")
    host.stop()


def main():
    test_disabled_by_default()
    test_plain_and_tidy_maps()
    test_legacy_map_migration_auto_enables()
    test_disable_auto_enabled_on_clear()
    test_player_host_auto_enables()
    print("\nALL AUTO-ENABLE TESTS PASSED")


if __name__ == "__main__":
    main()
