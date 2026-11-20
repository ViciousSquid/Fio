"""GLB material factors are coerced on load; a packaged .gltf is loaded safely.

Regressions covered:

* a ``baseColorFactor`` of ``[1]`` or ``["r", "g", "b", "a"]`` (or a string
  ``emissiveFactor``, a NaN ``alpha``) loaded "successfully", and then
  ``float(colour[1])`` raised in the renderer's draw-group resolution on
  every paintGL -- in Play, that ended the game;
* a ``.gltf`` read from a package was parsed by ``engine.model_source``
  itself, with no decode budget and no error handling: a malformed one raised
  into the caller instead of loading as "no model".
"""

import json
import math
import struct

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.glb_loader imports PyOpenGL")

from engine.glb_loader import GLBLoader          # noqa: E402
from engine.model_source import load_model       # noqa: E402

FLOAT = 5126
POSITIONS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], "<f4").tobytes()


def _gltf(materials):
    return {"asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(POSITIONS)}],
            "bufferViews": [{"buffer": 0, "byteLength": len(POSITIONS)}],
            "accessors": [{"bufferView": 0, "componentType": FLOAT,
                           "count": 3, "type": "VEC3"}],
            "materials": materials,
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0},
                                        "material": i}
                                       for i in range(len(materials))]}]}


def _glb(gltf, blob=POSITIONS):
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    chunks = (struct.pack("<II", len(js), 0x4E4F534A) + js
              + struct.pack("<II", len(blob), 0x004E4942) + blob)
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks


def _draw_values(material):
    """What the renderer reads from a material every frame
    (renderer/core/resources.py draw-group resolution)."""
    colour = material["color"]
    return (float(material.get("alpha", 1.0)),
            (float(colour[0]), float(colour[1]), float(colour[2])),
            [float(v) for v in material["emissive"]],
            float(material["metallic"]), float(material["roughness"]))


BAD_MATERIALS = [
    {"name": "short", "pbrMetallicRoughness": {"baseColorFactor": [0.5]}},
    {"name": "strings", "pbrMetallicRoughness": {"baseColorFactor": ["r", "g", "b", "a"]}},
    {"name": "scalar", "pbrMetallicRoughness": {"baseColorFactor": "red",
                                                "metallicFactor": "x",
                                                "roughnessFactor": None}},
    {"name": "emissive", "emissiveFactor": "glow"},
    {"name": "emissive_short", "emissiveFactor": [1, None]},
    {"name": "bools", "pbrMetallicRoughness": {"baseColorFactor": [True, False, 1, 1]}},
    {"name": "texture_index", "pbrMetallicRoughness": {"baseColorTexture": {"index": "0"}},
     "normalTexture": {"index": -1}, "emissiveTexture": "x"},
    "not a material",
    {"name": ["unhashable"]},
]


def test_malformed_factors_load_as_finite_defaults():
    loader = GLBLoader()
    assert loader.load_bytes(_glb(_gltf(BAD_MATERIALS)), "bad.glb")
    assert len(loader.materials) == len(BAD_MATERIALS)
    for material in loader.materials.values():
        alpha, colour, emissive, metallic, roughness = _draw_values(material)
        assert len(material["color"]) == 3 and len(emissive) == 3
        assert all(math.isfinite(v) for v in (alpha, *colour, *emissive, metallic, roughness))
        assert 0.0 <= alpha <= 1.0
    short = loader.materials["short"]
    assert short["color"] == [0.5, 1.0, 1.0] and short["alpha"] == 1.0
    assert loader.materials["strings"]["color"] == [1.0, 1.0, 1.0]
    assert loader.materials["emissive_short"]["emissive"] == [1.0, 0.0, 0.0]
    assert loader.materials["bools"]["color"] == [1.0, 1.0, 1.0]
    assert loader.materials["texture_index"]["texture"] is None


def test_non_finite_factors_are_replaced():
    # Python's json writes (and reads) NaN and Infinity.
    materials = [{"name": "nan", "pbrMetallicRoughness": {
        "baseColorFactor": [float("nan"), 0.25, float("inf"), float("nan")],
        "metallicFactor": float("inf")},
        "emissiveFactor": [float("-inf"), 0.5, 0.5]}]
    loader = GLBLoader()
    assert loader.load_bytes(_glb(_gltf(materials)), "nan.glb")
    material = loader.materials["nan"]
    assert material["color"] == [1.0, 0.25, 1.0]
    assert material["alpha"] == 1.0
    assert material["metallic"] == 0.0
    assert material["emissive"] == [0.0, 0.5, 0.5]


def test_well_formed_material_is_unchanged():
    materials = [{"name": "ok", "doubleSided": True, "pbrMetallicRoughness": {
        "baseColorFactor": [0.1, 0.2, 0.3, 0.4], "metallicFactor": 0.7,
        "roughnessFactor": 0.2}, "emissiveFactor": [0.5, 0.25, 0]}]
    loader = GLBLoader()
    assert loader.load_bytes(_glb(_gltf(materials)), "ok.glb")
    material = loader.materials["ok"]
    assert material["color"] == pytest.approx([0.1, 0.2, 0.3])
    assert material["alpha"] == pytest.approx(0.4)
    assert (material["metallic"], material["roughness"]) == (0.7, 0.2)
    assert material["emissive"] == [0.5, 0.25, 0.0]
    assert material["double_sided"] is True


class _Assets:
    """An asset source as a played package provides it."""

    def __init__(self, files):
        self.files = files

    def read_asset(self, path):
        return self.files.get(path)

    def read_text(self, path):
        data = self.files.get(path)
        return None if data is None else data.decode("utf-8")


def _gltf_document():
    import base64
    gltf = _gltf([{"name": "m", "pbrMetallicRoughness": {"baseColorFactor": [1]}}])
    gltf["buffers"] = [{"byteLength": len(POSITIONS),
                        "uri": "data:application/octet-stream;base64,"
                               + base64.b64encode(POSITIONS).decode()}]
    return gltf


def test_packaged_gltf_loads_through_load_bytes():
    assets = _Assets({"assets/models/tri.gltf": json.dumps(_gltf_document()).encode()})
    loader = load_model("assets/models/tri.gltf", assets)
    assert loader is not None
    assert len(loader.get_flattened_vertices()) == 3
    # Through the same material coercion as a .glb.
    assert loader.materials["m"]["color"] == [1.0, 1.0, 1.0]


@pytest.mark.parametrize("document", [
    b"{not json", b"[1, 2, 3]", b'"a string"',
    json.dumps({"buffers": [1], "meshes": "x"}).encode(),
    json.dumps({"accessors": [{"bufferView": 0, "componentType": FLOAT,
                               "count": 10 ** 9, "type": "VEC3"}],
                "bufferViews": [{"buffer": 0, "byteLength": 12}],
                "buffers": [{"byteLength": 12}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]}).encode(),
], ids=["syntax", "list", "string", "wrong-types", "huge-accessor"])
def test_malformed_packaged_gltf_is_no_model_not_an_exception(document):
    assets = _Assets({"assets/models/bad.gltf": document})
    loader = load_model("assets/models/bad.gltf", assets)
    assert loader is None or loader.get_flattened_vertices() == []


def test_packaged_gltf_reads_its_external_buffer():
    """A .gltf's data is in an external buffer read from the same package
    (and its decode budget follows that buffer's size, not the JSON's)."""
    count = 300_000
    blob = np.zeros((count, 3), "<f4").tobytes()
    gltf = {"asset": {"version": "2.0"},
            "buffers": [{"byteLength": len(blob), "uri": "tri.bin"}],
            "bufferViews": [{"buffer": 0, "byteLength": len(blob)}],
            "accessors": [{"bufferView": 0, "componentType": FLOAT,
                           "count": count, "type": "VEC3"}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]}
    assets = _Assets({"assets/models/big.gltf": json.dumps(gltf).encode(),
                      "assets/models/tri.bin": blob})
    loader = load_model("assets/models/big.gltf", assets)
    assert loader is not None
    assert len(loader.get_flattened_vertices()) == count
