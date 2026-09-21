"""
Player-host test for Tidy using core Props.

The player host now instantiates core Props and runs the shared PropSession, so
Tidy does not need its own pickup/carry/drop implementation.
"""

import contextlib
import os
import sys
import subprocess

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _BlockImports:
    def __init__(self, prefixes):
        self.prefixes = tuple(prefixes)

    def find_spec(self, name, path=None, target=None):
        if name in self.prefixes or name.startswith(tuple(p + "." for p in self.prefixes)):
            raise ModuleNotFoundError(f"blocked for player simulation: {name}")
        return None


@contextlib.contextmanager
def _simulate_player_process():
    blocker = _BlockImports(("editor", "PyQt5"))
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        try:
            sys.meta_path.remove(blocker)
        except ValueError:
            pass


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


_CLEAN_PROCESS = "PyQt5" not in sys.modules and "glm" not in sys.modules

_NO_HEAVY_IMPORTS_SCRIPT = """
import sys
sys.path.insert(0, %r)

class _Block:
    def __init__(self, prefixes):
        self.prefixes = tuple(prefixes)
    def find_spec(self, name, path=None, target=None):
        if name in self.prefixes or name.startswith(tuple(p + "." for p in self.prefixes)):
            raise ModuleNotFoundError("blocked for player simulation: " + name)
        return None

sys.meta_path.insert(0, _Block(("editor", "PyQt5")))
from plugins.manager import load_plugins, get_manager
load_plugins()
assert "tidy" in [p.name for p in get_manager().plugins]
assert "PyQt5" not in sys.modules
assert "glm" not in sys.modules
print("OK")
""" % (_ROOT,)


def test_plugin_loads_without_editor_or_pyqt():
    print("[1] player-safe plugin imports")
    if _CLEAN_PROCESS:
        from plugins.manager import load_plugins, get_manager
        load_plugins()
        _check("tidy" in [p.name for p in get_manager().plugins],
               "tidy plugin loaded in player mode")
        _check("PyQt5" not in sys.modules, "PyQt5 was never imported")
        _check("glm" not in sys.modules, "glm was never imported")
    else:
        result = subprocess.run(
            [sys.executable, "-c", _NO_HEAVY_IMPORTS_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        _check(
            result.returncode == 0 and "OK" in result.stdout,
            "player-mode import check passed in a clean interpreter",
        )


def test_player_host_runs_core_prop_and_tidy():
    print("[2] player host runs core Prop pickup + Tidy placement")
    from plugins.manager import get_manager
    from player.plugin_host import PlayerPluginHost

    mgr = get_manager()
    tidy = next(p for p in mgr.plugins if p.name == "tidy")
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
                    "model_path": "plugins/tidy/assets/book.obj",
                    "name": "b1",
                },
            },
            {
                "type": "tidyreceptacle",
                "pos": [0, 40, -60],
                "properties": {
                    "type": "tidyreceptacle",
                    "accepts": "book",
                    "name": "shelf",
                },
            },
            {
                "type": "tidygoal",
                "pos": [0, 40, 0],
                "properties": {
                    "type": "tidygoal",
                    "target": "all",
                    "name": "goal",
                },
            },
        ],
    }

    host = PlayerPluginHost()
    _check(host.load(package=None) is True, "host loaded plugins")
    host.build_and_start(map_data)

    _check(mgr.is_enabled(tidy) is True, "core-Prop Tidy metadata auto-enabled Tidy")
    _check(host.active is True, "host active")
    _check(len(host.things) == 3, "host built core Prop + Tidy entities")
    _check(host.bridge._props is not None, "shared core PropSession attached")

    cam = [0.0, 40.0, 0.0]
    host.tick(0.016, cam, 90.0, 0.0, use_pressed=True)
    session = host.bridge._tidy
    _check(session is not None, "Tidy session started")
    _check(host.bridge._props.held is not None, "core PropSession picked the object")

    host.tick(0.016, cam, -90.0, 0.0, use_pressed=True)
    _check(host.bridge._props.held is None, "core PropSession released the object")
    _check(session.tidied == 1, "Tidy progress advanced")
    _check("Tidied" in (host.hud_message or ""), "HUD shows Tidy progress")

    host.stop()
    _check(host.things[0].pos == [0, 40, 35], "core Prop restored authored position on stop")


def main():
    test_plugin_loads_without_editor_or_pyqt()
    test_player_host_runs_core_prop_and_tidy()
    print("\nALL PLAYER TESTS PASSED")


if __name__ == "__main__":
    main()
