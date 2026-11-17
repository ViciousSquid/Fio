"""A model file cannot make its loader read a file outside its assets folder.

``mtllib`` in an OBJ and a buffer ``uri`` in a glTF are relative references
written by whoever made the model. Before the fix an OBJ in a shared map
could name ``../../../outside/secret.mtl`` and the loader read it.
"""

import json
import struct

import pytest

pytest.importorskip("OpenGL", reason="engine.glb_loader imports PyOpenGL")

from engine.glb_loader import GLBLoader          # noqa: E402
from engine.obj_loader import OBJLoader          # noqa: E402

pytestmark = pytest.mark.qt

TRIANGLE = "v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl m\nf 1 2 3\n"
MTL = "newmtl m\nKd 0.1 0.2 0.3\nmap_Kd skin.png\n"


def _project(tmp_path):
    models = tmp_path / "proj" / "assets" / "models"
    models.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mtl").write_text(MTL)
    return models, outside


@pytest.mark.parametrize("reference", [
    "../../../outside/secret.mtl",
    "..\\..\\..\\outside\\secret.mtl",
    "{outside}/secret.mtl",                       # absolute
    "\\\\evil-host\\share\\secret.mtl",
])
def test_an_obj_cannot_load_a_material_library_outside_its_assets(tmp_path, capsys, reference):
    models, outside = _project(tmp_path)
    reference = reference.format(outside=outside)
    (models / "evil.obj").write_text(f"mtllib {reference}\n{TRIANGLE}")
    loader = OBJLoader()
    assert loader.load(str(models / "evil.obj")), "the geometry itself is still a valid model"
    assert loader.materials == {}, (
        f"mtllib {reference!r} was read from outside the model's assets: {loader.materials}")
    assert "Refused material library" in capsys.readouterr().out


def test_an_obj_still_loads_materials_beside_it_and_elsewhere_in_assets(tmp_path):
    models, _outside = _project(tmp_path)
    (models / "good.mtl").write_text(MTL)
    (models / "good.obj").write_text(f"mtllib good.mtl\n{TRIANGLE}")
    loader = OBJLoader()
    assert loader.load(str(models / "good.obj"))
    assert loader.materials["m"]["texture"] == "skin.png"

    shared = models.parent / "materials"
    shared.mkdir()
    (shared / "shared.mtl").write_text(MTL)
    (models / "sub").mkdir()
    (models / "sub" / "far.obj").write_text(f"mtllib ../../materials/shared.mtl\n{TRIANGLE}")
    loader = OBJLoader()
    assert loader.load(str(models / "sub" / "far.obj"))
    assert "m" in loader.materials, "a '..' that stays inside assets/ must keep working"


def test_a_package_obj_cannot_ask_its_reader_for_a_path_outside_assets():
    asked = []

    def read_text(path):
        asked.append(path)
        return MTL

    loader = OBJLoader()
    assert loader.load_text(f"mtllib ../../maps/secret.mtl\n{TRIANGLE}",
                            "assets/models/evil.obj", read_text)
    assert asked == [], f"the package reader was asked for {asked}"
    loader = OBJLoader()
    assert loader.load_text(f"mtllib good.mtl\n{TRIANGLE}", "assets/models/good.obj", read_text)
    assert [p.replace("\\", "/") for p in asked] == ["assets/models/good.mtl"]


def _gltf(uri, length):
    return {
        "asset": {"version": "2.0"},
        "buffers": [{"uri": uri, "byteLength": length}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": length}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "type": "VEC3", "count": 3}],
    }


def _buffers_for(uri, hint, payload):
    asked = []

    def reader(path):
        asked.append(path)
        return payload

    loader = GLBLoader()
    loader._filepath_hint, loader._asset_reader = hint, reader
    loader.json_data = json.loads(json.dumps(_gltf(uri, len(payload))))
    loader.buffers_json = loader.json_data["buffers"]
    loader.binary_blob = None
    loader._resolve_buffers()
    return loader.buffers, asked


@pytest.mark.parametrize("uri", [
    "../../../outside/secret.bin", "..\\..\\..\\secret.bin",
    "\\\\evil-host\\share\\secret.bin", "//evil-host/share/secret.bin",
    "/etc/passwd", "C:\\secret.bin",
])
def test_a_gltf_buffer_uri_cannot_leave_the_models_assets(uri):
    payload = struct.pack("<9f", *range(9))
    buffers, asked = _buffers_for(uri, "assets/models/evil.gltf", payload)
    assert asked == [], f"buffer uri {uri!r} was read as {asked}"
    assert buffers == [b""]


def test_a_gltf_buffer_beside_the_model_still_loads():
    payload = struct.pack("<9f", *range(9))
    buffers, asked = _buffers_for("mesh.bin", "assets/models/good.gltf", payload)
    assert [p.replace("\\", "/") for p in asked] == ["assets/models/mesh.bin"]
    assert buffers == [payload]
