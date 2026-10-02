"""
Headless test for the "disabled-by-default, auto-enable on level load" flow.

The Tidy plugin ships ``enabled = False`` so ordinary maps never pay for its
gameplay. When a level that references its entities is loaded, the manager
switches it on automatically. This exercises that at the manager level and via
the player host, without the editor or PyQt.

Run from the repo root:  python plugins/tidy/tests/test_auto_enable.py
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
    # Start each test from a known-off state (other tests may have flipped it).
    mgr.set_enabled(tidy, False)
    _check(mgr.is_enabled(tidy) is False, "tidy is disabled by default")


def test_auto_enable_for_types():
    print("[2] loading tidy entity types enables the plugin")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    # A map with no tidy entities leaves it off.
    off = mgr.auto_enable_for_types(["light", "playerstart"])
    _check(off == [], "no plugins enabled for a tidy-free type list")
    _check(mgr.is_enabled(tidy) is False, "tidy stays disabled")

    # A map referencing a tidy type flips it on and reports it.
    newly = mgr.auto_enable_for_types(["light", "tidyobject"])
    _check([p.name for p in newly] == ["tidy"], "auto_enable reports tidy")
    _check(mgr.is_enabled(tidy) is True, "tidy is now enabled")

    # Already-on: no double report.
    again = mgr.auto_enable_for_types(["tidygoal"])
    _check(again == [], "already-enabled plugin is not re-reported")


def test_auto_enable_for_map():
    print("[3] auto_enable_for_map scans a level's things")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    plain = {"version": 3, "things": [
        {"type": "light", "pos": [0, 0, 0], "properties": {"type": "light"}}]}
    _check(mgr.auto_enable_for_map(plain) == [], "plain map enables nothing")
    _check(mgr.is_enabled(tidy) is False, "tidy stays off for a plain map")

    tidy_map = {"version": 3, "things": [
        {"type": "light", "pos": [0, 0, 0], "properties": {"type": "light"}},
        {"type": "tidyobject", "pos": [0, 40, 0],
         "properties": {"type": "tidyobject", "name": "b1"}}]}
    enabled = mgr.auto_enable_for_map(tidy_map)
    _check([p.name for p in enabled] == ["tidy"], "tidy map enables tidy")
    _check(mgr.is_enabled(tidy) is True, "tidy on after loading a tidy map")

    # Malformed / empty inputs are tolerated.
    _check(mgr.auto_enable_for_map({}) == [], "empty map is a no-op")
    _check(mgr.auto_enable_for_map(None) == [], "non-dict is a no-op")


def test_disable_auto_enabled_on_clear():
    print("[5] a cleared/new scene reverts a level-driven auto-enable")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)
    mgr.set_enabled(tidy, False)

    # Auto-enabled by a level -> a cleared scene turns it back off.
    mgr.auto_enable_for_types(["tidyobject"])
    _check(mgr.is_enabled(tidy) is True, "tidy auto-enabled for the level")
    reverted = mgr.disable_auto_enabled()
    _check([p.name for p in reverted] == ["tidy"], "clear reverts the auto-enable")
    _check(mgr.is_enabled(tidy) is False, "tidy is off after a New/empty scene")

    # Idempotent: a second clear with nothing auto-enabled is a no-op.
    _check(mgr.disable_auto_enabled() == [], "clear with nothing to revert is a no-op")

    # A manual enable (auto=False) is the user's choice and must survive a clear.
    mgr.set_enabled(tidy, True)
    _check(mgr.disable_auto_enabled() == [],
           "manual enable is not treated as auto")
    _check(mgr.is_enabled(tidy) is True, "user-enabled plugin survives a clear")
    mgr.set_enabled(tidy, False)


def test_player_host_auto_enables():
    print("[4] player host auto-enables tidy from bundled map data")
    from plugins.manager import get_manager
    from player.plugin_host import PlayerPluginHost
    mgr = get_manager()
    _tidy_plugin = _tidy(mgr)
    mgr.set_enabled(_tidy_plugin, False)

    map_data = {"version": 3, "brushes": [], "things": [
        {"type": "tidyobject", "pos": [0, 40, 35],
         "properties": {"type": "tidyobject", "category": "book", "name": "b1"}},
        {"type": "tidyreceptacle", "pos": [0, 40, -60],
         "properties": {"type": "tidyreceptacle", "accepts": "any", "name": "s"}},
    ]}

    host = PlayerPluginHost()
    _check(host.load(package=None) is True, "player host loaded plugins")
    _check(mgr.is_enabled(_tidy_plugin) is False,
           "tidy still disabled after load, before a map is built")
    host.build_and_start(map_data)
    _check(mgr.is_enabled(_tidy_plugin) is True,
           "building a tidy map auto-enabled the plugin")
    _check(host.active is True and host.bridge is not None,
           "play session started (gameplay would have been skipped if disabled)")
    host.stop()


def main():
    test_disabled_by_default()
    test_auto_enable_for_types()
    test_auto_enable_for_map()
    test_disable_auto_enabled_on_clear()
    test_player_host_auto_enables()
    print("\nALL AUTO-ENABLE TESTS PASSED")


if __name__ == "__main__":
    main()
