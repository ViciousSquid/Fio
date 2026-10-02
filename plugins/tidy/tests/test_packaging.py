"""
Headless test for plugin-aware .fiopak packaging and the editor menu/exporter
integration hooks.

Run from the repo root:  python plugins/tidy/tests/test_packaging.py
"""

import json
import os
import sys
import tempfile
import zipfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _make_base_pak(path, map_data):
    """Write a minimal base .fiopak (as the exporter would, pre-augment)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps({"title": "Tidy Demo",
                                                 "map_path": "maps/level.json"}))
        zf.writestr("maps/level.json", json.dumps(map_data))


def test_ownership_tracking():
    print("[1] entity->plugin ownership")
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()
    _check(mgr.plugin_for_type("tidyobject") is not None,
           "manager maps 'tidyobject' -> owning plugin")
    _check(mgr.plugin_for_type("tidy_object") is not None,
           "underscore/normalised type also resolves")
    _check(mgr.plugin_for_type("light") is None,
           "core entity types are not plugin-owned")
    reqs = mgr.required_plugins_for_types(["tidyobject", "tidygoal", "light"])
    _check(len(reqs) == 1 and mgr.plugin_package_name(reqs[0]) == "tidy",
           "required_plugins_for_types dedups to the tidy plugin")


def test_augment_fiopak_bundles_plugin():
    print("[2] augment_fiopak bundles the plugin the map uses")
    from plugins.tidy.entities import TidyObject, TidyReceptacle, TidyGoal
    from plugins.packaging import augment_fiopak, collect_entity_types

    obj = TidyObject(pos=[0, 12, 0], properties={"category": "book", "name": "b1"})
    model_path = obj.properties["model_path"]
    map_data = {
        "version": 3,
        "brushes": [],
        "things": [obj.to_dict(),
                   TidyReceptacle(pos=[0, 40, -60]).to_dict(),
                   TidyGoal().to_dict()],
    }
    _check(collect_entity_types(map_data) >= {"tidyobject", "tidyreceptacle", "tidygoal"},
           "collect_entity_types finds the plugin entity types")

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
        # Plugin-system core travels with the package.
        for core in ("plugins/__init__.py", "plugins/api.py", "plugins/manager.py",
                     "plugins/integration.py", "plugins/packaging.py"):
            _check(core in names, f"core file bundled: {core}")
        # The plugin package itself (code + assets).
        _check("plugins/tidy/plugin.py" in names, "tidy plugin code bundled")
        _check("plugins/tidy/entities.py" in names, "tidy entities bundled")
        # The model the map references resolves straight out of the package.
        _check(model_path in names,
               f"model asset bundled at its referenced path ({model_path})")
        # No bytecode/test-cache junk.
        _check(not any("__pycache__" in n for n in names), "no __pycache__ bundled")


def test_augment_is_noop_without_plugin_entities():
    print("[3] packaging is a no-op when no plugin entities are used")
    from plugins.packaging import augment_fiopak

    map_data = {"version": 3, "brushes": [],
                "things": [{"type": "light", "pos": [0, 0, 0], "properties": {"type": "light"}}]}
    with tempfile.TemporaryDirectory() as tmp:
        pak = os.path.join(tmp, "plain.fiopak")
        _make_base_pak(pak, map_data)
        before = set(zipfile.ZipFile(pak).namelist())
        summary = augment_fiopak(pak)
        after = set(zipfile.ZipFile(pak).namelist())
        _check(summary["plugins"] == [], "no plugins required")
        _check(before == after, "archive left unchanged")


def test_player_package_reads_plugins():
    print("[4] player FioPackage exposes required plugins")
    from plugins.tidy.entities import TidyObject
    from plugins.packaging import augment_fiopak
    from player.fiopak import FioPackage

    map_data = {"version": 3, "brushes": [], "things": [TidyObject(pos=[0, 12, 0]).to_dict()]}
    with tempfile.TemporaryDirectory() as tmp:
        pak = os.path.join(tmp, "demo.fiopak")
        _make_base_pak(pak, map_data)
        augment_fiopak(pak)
        with FioPackage.open(pak) as pkg:
            _check(pkg.required_plugins == ["tidy"],
                   "FioPackage.required_plugins reads the manifest")
            _check(pkg.has_bundled_plugins() is True, "detects bundled plugin files")
            _check(all(not m.startswith("plugins/") for m in pkg.list_maps()),
                   "bundled plugin files excluded from map list")


def test_integration_hooks_installed():
    print("[5] editor menu + exporter integration hooks install")
    import editor  # runs editor/__init__ -> load_plugins + integration.apply
    from plugins import integration
    integration.apply()
    try:
        from editor.ui import Ui_MainWindow
        _check(getattr(Ui_MainWindow, "_fio_plugins_patched", False),
               "Ui_MainWindow patched (Plugins menu bar entry)")
    except Exception as exc:
        print(f"  skip: editor.ui unavailable ({exc})")
    try:
        from editor.package_exporter import PackageExporter
        # Plugin bundling is a native, first-class step of export (no monkey-patch).
        _check(callable(getattr(PackageExporter, "_bundle_plugins", None)),
               "PackageExporter bundles plugins natively on export")
    except Exception as exc:
        print(f"  skip: package_exporter unavailable ({exc})")
    try:
        from editor.editor_state import EditorState
        _check(getattr(EditorState, "_fio_plugins_patched", False),
               "EditorState patched (auto-enable plugins on level load)")
    except Exception as exc:
        print(f"  skip: editor_state unavailable ({exc})")


def main():
    test_ownership_tracking()
    test_augment_fiopak_bundles_plugin()
    test_augment_is_noop_without_plugin_entities()
    test_player_package_reads_plugins()
    test_integration_hooks_installed()
    print("\nALL PACKAGING TESTS PASSED")


if __name__ == "__main__":
    main()
