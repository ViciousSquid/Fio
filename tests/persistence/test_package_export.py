"""What Export Game Package writes, checked by reading the archive back.

The exporter had no behavioural coverage, and several of its promises were not
kept: the manifest was written before its ``map_path`` was filled in, a level
change's target map was only followed when the dialog supplied a start map
(it never does), and asset references were copied from wherever they pointed —
an absolute path or a ``../`` climb produced an archive entry outside the
package root.  Every test here builds a small project on disk, exports it, and
opens the result with the player's own reader.
"""

import json
import os
import zipfile

import pytest

from editor.package_exporter import PackageExporter
from player.fiopak import FioPackage


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _level(things=(), textures=None):
    brush = {"id": "b1", "pos": [0, 0, 0], "size": [64, 64, 64]}
    if textures is not None:
        brush["textures"] = textures
    return {"version": 3, "brushes": [brush], "things": list(things)}


def _changer(target):
    return {"type": "LevelChanger", "pos": [0, 0, 0],
            "properties": {"type": "LevelChanger", "target_map": target}}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "assets" / "textures").mkdir(parents=True)
    (root / "assets" / "textures" / "floor.png").write_bytes(b"floor")
    (root / "assets" / "sounds").mkdir(parents=True)
    (root / "assets" / "sounds" / "beep.wav").write_bytes(b"beep")
    return root


def _export(root, current, out, metadata=None):
    exporter = PackageExporter(None, str(root))
    ok, errors = exporter.export(str(out), dict(metadata or {"title": "T"}),
                                 str(current))
    return ok, errors


def test_manifest_names_the_entry_map(project, tmp_path):
    current = _write_json(project / "maps" / "start.json", _level())
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok, errors
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("metadata.json"))
    assert manifest["map_path"] == "maps/start.json", (
        "metadata.json was written before map_path was set")
    with FioPackage.open(str(out)) as pak:
        assert pak.start_map_path() == "maps/start.json"


def test_level_change_targets_are_packaged(project, tmp_path):
    """The dialog supplies no start map; the current map's links still count."""
    _write_json(project / "maps" / "second.json", _level([_changer("third.json")]))
    _write_json(project / "maps" / "third.json", _level())
    current = _write_json(project / "maps" / "first.json",
                          _level([_changer("maps/second.json")]))
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok, errors
    with FioPackage.open(str(out)) as pak:
        assert pak.list_maps() == ["maps/first.json", "maps/second.json",
                                   "maps/third.json"]
        assert pak.start_map_path() == "maps/first.json"


def test_referenced_assets_are_packaged_under_assets(project, tmp_path):
    level = _level([{"type": "Speaker", "pos": [0, 0, 0],
                     "properties": {"type": "Speaker", "sound_file": "beep.wav"}}],
                   textures={"top": "floor.png", "bottom": "missing.png"})
    current = _write_json(project / "maps" / "start.json", level)
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok
    with FioPackage.open(str(out)) as pak:
        assert pak.read_asset("floor.png") == b"floor"
        assert pak.read_asset("beep.wav") == b"beep"
    assert any("missing.png" in e for e in errors)


@pytest.mark.parametrize("reference", ["../../secret.txt",
                                       "textures/../../../secret.txt"])
def test_relative_references_cannot_leave_the_project(project, tmp_path, reference):
    (tmp_path / "secret.txt").write_bytes(b"do not ship")
    level = _level(textures={"top": reference})
    current = _write_json(project / "maps" / "start.json", level)
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        payloads = [zf.read(n) for n in names]
    assert all(".." not in n.split("/") and not n.startswith("/") for n in names), names
    assert b"do not ship" not in payloads
    assert any("outside the project" in e for e in errors), errors


def test_absolute_references_cannot_leave_the_project(project, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"do not ship")
    outside_map = _write_json(tmp_path / "elsewhere.json", _level())
    level = _level([_changer(str(outside_map))], textures={"top": str(secret)})
    current = _write_json(project / "maps" / "start.json", level)
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok
    with zipfile.ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ["maps/start.json", "metadata.json"]
        assert b"do not ship" not in [zf.read(n) for n in zf.namelist()]
    assert sum("outside the project" in e for e in errors) == 2, errors


def test_a_failed_export_leaves_the_previous_package_intact(project, tmp_path,
                                                            monkeypatch):
    current = _write_json(project / "maps" / "start.json",
                          _level(textures={"top": "floor.png"}))
    out = tmp_path / "game.fiopak"
    assert _export(project, current, out)[0]
    before = out.read_bytes()

    real_write = zipfile.ZipFile.write

    def failing_write(self, filename, arcname=None, *args, **kwargs):
        if str(arcname).startswith("assets/"):
            raise OSError("disk full")
        return real_write(self, filename, arcname, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", failing_write)
    ok, errors = _export(project, current, out)

    assert not ok
    assert out.read_bytes() == before, "a failed export truncated the package"
    assert [p for p in os.listdir(tmp_path) if p.endswith(".tmp")] == []


def test_map_name_collisions_get_distinct_archive_paths(project, tmp_path):
    _write_json(project / "maps" / "sub" / "start.json", _level())
    current = _write_json(project / "maps" / "start.json",
                          _level([_changer("sub/start.json")]))
    out = tmp_path / "game.fiopak"

    ok, errors = _export(project, current, out)

    assert ok, errors
    with FioPackage.open(str(out)) as pak:
        assert pak.list_maps() == ["maps/start.json", "maps/start_1.json"]
        assert pak.start_map_path() == "maps/start.json"
