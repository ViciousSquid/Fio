"""Regression tests for the no-plugin-code .fiopak boundary."""

import io
import json
import zipfile

import pytest

from player.fiopak import FioPackage, PackageError


def _bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in entries.items():
            zf.writestr(name, raw)
    return buf.getvalue()


def test_package_plugin_code_is_rejected():
    data = _bytes({
        "metadata.json": json.dumps({"title": "Demo"}).encode(),
        "maps/start.json": b'{"version":3,"things":[]}',
        "plugins/packagedemo/__init__.py": b"raise RuntimeError",
    })
    with pytest.raises(PackageError, match="Bundled plugins are not permitted"):
        FioPackage.from_bytes(data)


def test_package_plugin_dependency_metadata_is_not_code():
    data = _bytes({
        "metadata.json": json.dumps({
            "title": "Demo",
            "plugins": ["packagedemo"],
        }).encode(),
        "maps/start.json": b'{"version":3,"things":[]}',
    })
    with FioPackage.from_bytes(data) as package:
        assert package.required_plugins == ["packagedemo"]


def test_package_rejection_applies_to_backslash_entries_too():
    data = _bytes({
        "metadata.json": b'{"title":"Demo"}',
        "maps/start.json": b'{"version":3,"things":[]}',
        r"plugins\packagedemo\plugin.py": b"x",
    })
    with pytest.raises(PackageError, match="Bundled plugins are not permitted"):
        FioPackage.from_bytes(data)
