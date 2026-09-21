"""
Headless tests for plugin-aware .fiopak packaging after Tidy became a
core-Prop extension.
"""

import json
import os
import sys
import tempfile
import zipfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _make_base_pak(path, map_data):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "metadata.json",
            json.dumps({"title": "Tidy Demo", "map_path": "maps/level.json"}),
        )
        zf.writestr("maps/level.json", json.dumps(map_data))


def test_required_plugin_resolution():
    print("[1] core Props can require the Tidy plugin without owning the Prop type")
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()

    _check(mgr.plugin_for_type("prop") is None,
           "core Prop is not plugin-owned")
    map_data = {
        "version": 3,
        "things": [{
            "type": "prop",
            "properties": {"type": "prop", "tidy_category": "book"},
        }],
    }
    required = mgr.required_plugins_for_map(map_data)
    _check([p.name for p in required] == ["tidy"],
           "marked core Prop resolves to the Tidy plugin")


def test_augment_fiopak_bundles_plugin():
    print("[2] augment_fiopak bundles Tidy for marked core Props")
    from plugins.entitybase import Prop
    from plugins.tidy.entities import TidyReceptacle, TidyGoal
    from plugins.packaging import augment_fiopak, collect_entity_types

    obj = Prop(
        pos=[0, 12, 0],
        properties={
            "name": "b1",
            "tidy_category": "book",
            "model_path": "plugins/tidy/assets/book.obj",
        },
    )
    map_data = {
        "version": 3,
        "brushes": [],
        "things": [
            obj.to_dict(),
            TidyReceptacle(pos=[0, 40, -60]).to_dict(),
            TidyGoal().to_dict(),
        ],
    }
    model_path = obj.properties["model_path"]

    _check(collect_entity_types(map_data) >= {"prop", "tidyreceptacle", "tidygoal"},
           "collect_entity_types sees the core Prop + Tidy entities")

    with tempfile.TemporaryDirectory() as tmp:
        pak = os.path.join(tmp, "demo.fiopak")
        _make_base_pak(pak, map_data)
        summary = augment_fiopak(pak)

        _check(summary["plugins"] == ["tidy"], "summary reports the tidy plugin")

        with zipfile.ZipFile(pak) as zf:
            names = set(zf.namelist())
            meta = json.loads(zf.read("metadata.json").decode())

        _check(meta.get("plugins") == ["tidy"], "metadata.json records plugins")
        _check(meta.get("requires_plugins") is True, "metadata flags requires_plugins")
        for core in (
            "plugins/__init__.py",
            "plugins/api.py",
            "plugins/manager.py",
            "plugins/integration.py",
            "plugins/packaging.py",
        ):
            _check(core in names, f"core file bundled: {core}")
        _check("plugins/tidy/plugin.py" in names, "tidy plugin code bundled")
        _check("plugins/tidy/entities.py" in names, "tidy entities bundled")
        _check(model_path in names,
               f"model asset bundled at its referenced path ({model_path})")
        _check(not any("__pycache__" in n for n in names), "no __pycache__ bundled")


def test_augment_is_noop_without_tidy_metadata():
    print("[3] ordinary core Props do not pull Tidy into a package")
    from plugins.packaging import augment_fiopak

    map_data = {
        "version": 3,
        "brushes": [],
        "things": [{
            "type": "prop",
            "pos": [0, 0, 0],
            "properties": {"type": "prop", "name": "barrel"},
        }],
    }
    with tempfile.TemporaryDirectory() as tmp:
        pak = os.path.join(tmp, "plain.fiopak")
        _make_base_pak(pak, map_data)
        before = set(zipfile.ZipFile(pak).namelist())
        summary = augment_fiopak(pak)
        after = set(zipfile.ZipFile(pak).namelist())
        _check(summary["plugins"] == [], "no plugin required")
        _check(before == after, "archive left unchanged")


def test_player_package_reads_plugins():
    print("[4] player package receives Tidy from core Prop metadata")
    from plugins.entitybase import Prop
    from plugins.packaging import augment_fiopak
    from player.fiopak import FioPackage

    obj = Prop(
        pos=[0, 12, 0],
        properties={"name": "book1", "tidy_category": "book"},
    )
    map_data = {"version": 3, "brushes": [], "things": [obj.to_dict()]}

    with tempfile.TemporaryDirectory() as tmp:
        pak = os.path.join(tmp, "demo.fiopak")
        _make_base_pak(pak, map_data)
        augment_fiopak(pak)
        with FioPackage.open(pak) as pkg:
            _check(pkg.required_plugins == ["tidy"],
                   "FioPackage.required_plugins reads the manifest")
            _check(pkg.has_bundled_plugins() is True,
                   "detects bundled plugin files")
            _check(all(not m.startswith("plugins/") for m in pkg.list_maps()),
                   "bundled plugin files excluded from map list")


def main():
    test_required_plugin_resolution()
    test_augment_fiopak_bundles_plugin()
    test_augment_is_noop_without_tidy_metadata()
    test_player_package_reads_plugins()
    print("\nALL PACKAGING TESTS PASSED")


if __name__ == "__main__":
    main()
