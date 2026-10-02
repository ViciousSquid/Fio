"""
Player-runtime test: simulates the standalone .fiopak player, where neither the
editor package nor PyQt5/PyGLM exist, and verifies the tidy plugin still loads
and *runs* via the player plugin host.

Blocks ``editor`` and ``PyQt5`` imports up front so the plugin must fall back to
the dependency-free entity base, then drives pick-up/put-away through
``player.plugin_host.PlayerPluginHost`` against a camera bridge.

Run from the repo root:  python plugins/tidy/tests/test_player.py
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _BlockImports:
    """Meta-path finder that makes chosen top-level packages unimportable."""

    def __init__(self, prefixes):
        self.prefixes = tuple(prefixes)

    def find_spec(self, name, path=None, target=None):
        if name in self.prefixes or name.startswith(tuple(p + "." for p in self.prefixes)):
            raise ModuleNotFoundError(f"blocked for player simulation: {name}")
        return None


# Install BEFORE importing plugins so entities.py takes the PyQt-free fallback.
sys.meta_path.insert(0, _BlockImports(("editor", "PyQt5")))


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_plugin_loads_without_editor_or_pyqt():
    print("[1] plugin loads with no editor / PyQt / glm")
    from plugins.manager import load_plugins, get_manager
    load_plugins()
    mgr = get_manager()
    _check("tidy" in [p.name for p in mgr.plugins], "tidy plugin loaded in player mode")
    _check("PyQt5" not in sys.modules, "PyQt5 was never imported")
    _check("glm" not in sys.modules, "glm was never imported")

    from plugins.tidy.entities import TidyObject
    from plugins.entitybase import Thing as BaseThing
    _check(issubclass(TidyObject, BaseThing),
           "TidyObject fell back to the dependency-free base")
    # The manager can still resolve type -> class without the editor palette.
    _check(mgr.entity_class_for_type("tidyobject") is TidyObject,
           "manager resolves entity class in player mode")


def test_player_host_runs_gameplay():
    print("[2] player host loads + runs the tidy session")
    from player.plugin_host import PlayerPluginHost

    map_data = {
        "version": 3,
        "brushes": [],
        "things": [
            {"type": "tidyobject", "pos": [0, 40, 35],
             "properties": {"type": "tidyobject", "category": "book", "name": "b1"}},
            {"type": "tidyreceptacle", "pos": [0, 40, -60],
             "properties": {"type": "tidyreceptacle", "accepts": "any", "name": "shelf"}},
            {"type": "tidygoal", "pos": [0, 40, 0],
             "properties": {"type": "tidygoal", "target": "all", "name": "goal"}},
        ],
    }

    host = PlayerPluginHost()
    # No package object needed: the app already has the plugin system importable.
    _check(host.load(package=None) is True, "host loaded plugins")
    host.build_and_start(map_data)
    _check(host.active is True, "host active with 1 tidy object in the map")
    _check(len(host.things) == 3, "host built the plugin entity instances")

    cam = [0.0, 40.0, 0.0]
    # Look +Z (engine angle 0 => cam_yaw 90) at the object and press USE.
    host.tick(0.016, cam, 90.0, 0.0, use_pressed=True)
    session = host.bridge._tidy
    _check(session.held is not None, "object picked up in the player")

    # Turn to face the shelf at -Z (cam_yaw -90) and press USE.
    host.tick(0.016, cam, -90.0, 0.0, use_pressed=True)
    _check(session.held is None, "object placed in the player")
    _check(session.tidied == 1, "tidied counter advanced")

    # An idle frame (looking up at nothing) surfaces the live progress counter.
    host.tick(0.016, cam, -90.0, 89.0, use_pressed=False)
    _check("Tidied" in (host.hud_message or ""),
           f"idle HUD shows progress ({host.hud_message!r})")

    host.stop()
    # Editor position restored (object sent home on stop).
    _check(host.things[0].pos == [0, 40, 35], "object restored home on stop")


def main():
    test_plugin_loads_without_editor_or_pyqt()
    test_player_host_runs_gameplay()
    print("\nALL PLAYER TESTS PASSED")


if __name__ == "__main__":
    main()
