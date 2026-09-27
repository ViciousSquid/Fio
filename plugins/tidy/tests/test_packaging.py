"""
Headless tests for the .fiopak plugin boundary.

Plugins are runtime dependencies, never archive payloads.
"""

import io
import json
import os
import sys
import zipfile

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _pak_with(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in entries.items():
            zf.writestr(name, raw)
    return buf.getvalue()


def test_required_plugin_resolution():
    print("[1] core Props can require the Tidy plugin without owning the Prop type")
    from plugins.manager import get_manager, load_plugins
    load_plugins()
    mgr = get_manager()

    _check(mgr.plugin_for_type("prop") is None, "core Prop is not plugin-owned")
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


def test_plugin_payload_is_rejected_by_fiopak():
    print("[2] .fiopak rejects executable plugin payloads")
    from player.fiopak import FioPackage, PackageError

    data = _pak_with({
        "metadata.json": json.dumps({"title": "Tidy Demo"}).encode(),
        "maps/level.json": b'{"version":3,"things":[]}',
        "plugins/tidy/plugin.py": b"PLUGIN = None",
    })
    try:
        FioPackage.from_bytes(data)
    except PackageError as exc:
        _check("Bundled plugins are not permitted" in str(exc),
               "bundled plugin payload is rejected")
    else:
        raise AssertionError("plugin-bearing .fiopak unexpectedly opened")


def test_plugin_dependency_metadata_is_allowed():
    print("[3] plugin dependency names may be recorded without code")
    from player.fiopak import FioPackage

    data = _pak_with({
        "metadata.json": json.dumps({
            "title": "Tidy Demo",
            "plugins": ["tidy"],
        }).encode(),
        "maps/level.json": b'{"version":3,"things":[]}',
    })
    with FioPackage.from_bytes(data) as pkg:
        _check(pkg.required_plugins == ["tidy"],
               "manifest dependency is readable")
        _check(pkg.list_maps() == ["maps/level.json"],
               "plain world entries remain portable")


def main():
    test_required_plugin_resolution()
    test_plugin_payload_is_rejected_by_fiopak()
    test_plugin_dependency_metadata_is_allowed()
    print("\nALL PACKAGING TESTS PASSED")


if __name__ == "__main__":
    main()
