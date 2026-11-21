"""Model companions are found as the loaders read them, and one odd model
does not abort a package export.

Regressions covered:

* ``companion_assets`` read OBJ and MTL lines with ``shlex.split``: an
  apostrophe (``o Bob's_chair``) raised "No closing quotation", a backslash
  path (``mtllib ..\\materials\\chair.mtl``) lost its separators, and a
  9.8 MB OBJ took 3.9 s to scan;
* a glTF with ``buffers: [1]``, a corrupt GLB or an absolute companion path
  raised out of ``companion_assets`` and ended the whole export;
* the exporter read each map about five times, and every asset -- textures
  and sounds included -- once more just to look for companions.
"""

import json
import struct
import zipfile

import pytest

from editor import package_exporter
from editor.package_exporter import PackageExporter
from engine.package_assets import companion_assets


def _glb(document):
    js = json.dumps(document).encode()
    js += b" " * (-len(js) % 4)
    chunk = struct.pack("<II", len(js), 0x4E4F534A) + js
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunk)) + chunk


@pytest.mark.parametrize("text", [
    "o Bob's_chair\nv 0 0 0\nmtllib chair.mtl\n",
    'g "unclosed\nmtllib chair.mtl\n',
    "# it's a comment\nmtllib chair.mtl\n",
])
def test_quotes_in_an_obj_do_not_raise(text):
    assert companion_assets("assets/models/x.obj", text.encode()) == [
        "assets/models/chair.mtl"]


def test_a_backslash_path_keeps_its_folders():
    assert companion_assets("assets/models/x.obj",
                            b"mtllib ..\\materials\\chair.mtl\n") == [
        "assets/materials/chair.mtl"]
    assert companion_assets("assets/models/x.mtl",
                            b"newmtl m\nmap_Kd -s 1 1 1 tex\\wood.png\n") == [
        "assets/models/tex/wood.png"]


def test_an_mtllib_name_runs_to_the_end_of_the_line_as_the_loader_reads_it():
    """OBJLoader joins everything after ``mtllib`` into one name; the
    package must hold that file, not the words of it."""
    assert companion_assets("assets/models/x.obj", b"mtllib my chair.mtl\n") == [
        "assets/models/my chair.mtl"]


def test_mtl_maps_bump_and_options_are_found():
    data = (b"newmtl a\nmap_Kd a.png\nbump -bm 0.5 n.png\n# map_Kd no.png\n"
            b"norm nn.png\nKd 1 1 1\n")
    assert companion_assets("assets/models/x.mtl", data) == [
        "assets/models/a.png", "assets/models/n.png", "assets/models/nn.png"]


@pytest.mark.parametrize("data, suffix", [
    (json.dumps({"buffers": [1, "x", {"uri": 5}, {"uri": "ok.bin"}]}).encode(), "gltf"),
    (json.dumps({"buffers": {"uri": "x.bin"}, "images": [{"uri": "ok.bin"}]}).encode(), "gltf"),
    (_glb({"buffers": [None], "images": [{"uri": "ok.bin"}]}), "glb"),
])
def test_odd_gltf_records_are_skipped_not_raised(data, suffix):
    assert companion_assets(f"assets/models/m.{suffix}", data) == ["assets/models/ok.bin"]


@pytest.mark.parametrize("path, data, message", [
    ("assets/models/m.glb", b"not a glb at all", "Invalid GLB"),
    ("assets/models/m.gltf", b"{broken", "Invalid glTF"),
    ("assets/models/m.gltf", b"[1, 2]", "Invalid glTF"),
    ("assets/models/m.obj", b"mtllib C:\\Users\\me\\chair.mtl\n", "project-relative"),
    ("assets/models/m.obj", b"mtllib /etc/chair.mtl\n", "project-relative"),
])
def test_a_bad_companion_is_reported_with_an_errors_list_and_raises_without(
        path, data, message):
    errors = []
    assert companion_assets(path, data, errors) == []
    assert len(errors) == 1 and message in errors[0]
    with pytest.raises(ValueError, match=message):
        companion_assets(path, data)


def test_a_large_obj_is_scanned_without_a_shell_word_parser():
    """Lines are split on whitespace (as OBJLoader does), not as shell words."""
    from engine import package_assets
    assert not hasattr(package_assets, "shlex")
    data = (b"o it's\n" + b"v 0.1 1.2 2.3\n" * 50_000
            + b"f 1/1/1 2/2/2 3/3/3\n" * 50_000 + b"mtllib a.mtl\n")
    assert companion_assets("assets/models/x.obj", data) == ["assets/models/a.mtl"]
    assert companion_assets("assets/models/x.obj", data.replace(b"mtllib", b"#")) == []


# -- the exporter -----------------------------------------------------------------

@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    models = root / "assets" / "models"
    models.mkdir(parents=True)
    (root / "assets" / "textures").mkdir(parents=True)
    (root / "assets" / "textures" / "floor.png").write_bytes(b"floor")
    (models / "chair.obj").write_text("o Bob's_chair\nmtllib chair.mtl\n"
                                      "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (models / "chair.mtl").write_text("newmtl m\nmap_Kd wood.png\n")
    (models / "wood.png").write_bytes(b"wood")
    (models / "broken.glb").write_bytes(b"glTF garbage")
    (models / "odd.gltf").write_text(json.dumps({"buffers": [1]}))
    (models / "far.obj").write_text("mtllib C:\\elsewhere\\far.mtl\n")
    (root / "maps").mkdir()
    things = [{"type": "Model", "pos": [0, 0, 0],
               "properties": {"type": "Model", "model_path": f"assets/models/{name}"}}
              for name in ("chair.obj", "broken.glb", "odd.gltf", "far.obj")]
    level = {"version": 3, "things": things,
             "brushes": [{"id": "b", "pos": [0, 0, 0], "size": [64, 64, 64],
                          "textures": {"north": "floor.png"}}]}
    (root / "maps" / "start.json").write_text(json.dumps(level))
    return root


def test_odd_models_are_reported_and_the_package_is_still_written(project, tmp_path):
    out = tmp_path / "game.fiopak"
    ok, errors = PackageExporter(None, str(project)).export(
        str(out), {"title": "T"}, str(project / "maps" / "start.json"))
    assert ok, errors
    assert any("Invalid GLB" in e for e in errors), errors
    assert any("project-relative" in e for e in errors), errors
    with zipfile.ZipFile(out) as package:
        names = set(package.namelist())
    assert {"assets/models/chair.obj", "assets/models/chair.mtl",
            "assets/models/wood.png", "assets/models/broken.glb",
            "assets/models/odd.gltf", "assets/textures/floor.png"} <= names


def test_each_map_is_read_once_and_only_models_are_scanned(project, tmp_path, monkeypatch):
    opened = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(package_exporter, "open", counting_open, raising=False)
    from engine import package_assets
    read = []
    real_read = package_assets.ProjectAssets.read

    def counting_read(self, name):
        read.append(name)
        return real_read(self, name)

    monkeypatch.setattr(package_assets.ProjectAssets, "read", counting_read)
    scanned = []
    real_scan = package_assets.companion_assets
    monkeypatch.setattr(package_assets, "companion_assets",
                        lambda name, data, errors=None: (scanned.append(name),
                                                         real_scan(name, data, errors))[1])

    out = tmp_path / "game.fiopak"
    ok, errors = PackageExporter(None, str(project)).export(
        str(out), {"title": "T"}, str(project / "maps" / "start.json"))

    assert ok, errors
    start = str(project / "maps" / "start.json")
    assert opened.count(start) == 1, opened
    assert "assets/textures/floor.png" not in scanned
    assert "assets/models/wood.png" not in scanned
    assert set(scanned) == {"assets/models/chair.obj", "assets/models/chair.mtl",
                            "assets/models/broken.glb", "assets/models/odd.gltf",
                            "assets/models/far.obj"}
    # Loose files are copied with zf.write, so a texture is never read
    # through the asset source at all.
    assert "assets/textures/floor.png" not in read
