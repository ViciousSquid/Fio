"""A GLB without normals is drawn flat-shaded, as glTF 2.0 requires.

glTF 2.0 (3.7.2.1): "When normals are not specified, client implementations
MUST calculate flat normals." The desktop loader gave every such vertex the
normal (0, 1, 0) instead: the model was lit as one flat colour whatever way a
face pointed, and a spinning model turned that "up" with it -- the bundled
tape reels (assets/bundled_reel_to_reel.fiopak, no NORMAL attribute) went
dark and light once a turn. The Android player already made flat normals
(player/render/models.py).
"""

import json
import struct
import types

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.glb_loader imports PyOpenGL")

from engine import glb_loader                      # noqa: E402
from engine.glb_loader import GLB, flat_shaded     # noqa: E402

pytestmark = pytest.mark.qt

FLOAT, USHORT = 5126, 5123

#: A unit cube's eight corners and its twelve triangles, wound outwards.
CUBE = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float32)
CUBE_TRIANGLES = [
    (0, 1, 3), (0, 3, 2),   # -X
    (4, 6, 7), (4, 7, 5),   # +X
    (0, 4, 5), (0, 5, 1),   # -Y
    (2, 3, 7), (2, 7, 6),   # +Y
    (0, 2, 6), (0, 6, 4),   # -Z
    (1, 5, 7), (1, 7, 3),   # +Z
]


#: A loose triangle in the X-Y plane, wound to face +Z, plus one stray vertex.
LOOSE = np.array([[0, 0, 5], [1, 0, 5], [0, 1, 5], [9, 9, 9]], np.float32)


def _cube_glb(loose_first=False):
    """A cube with POSITION and indices only, the way the reel pack is made;
    with *loose_first*, a non-indexed triangle in a mesh of its own before it."""
    vertex = CUBE.tobytes()
    index = np.array(CUBE_TRIANGLES, np.uint16).reshape(-1).tobytes()
    index += b"\0" * (-len(index) % 4)
    blob = vertex + index + LOOSE.tobytes()
    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertex)},
            {"buffer": 0, "byteOffset": len(vertex), "byteLength": len(index)},
            {"buffer": 0, "byteOffset": len(vertex) + len(index),
             "byteLength": LOOSE.nbytes},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": FLOAT, "count": 8, "type": "VEC3"},
            {"bufferView": 1, "componentType": USHORT, "count": 36, "type": "SCALAR"},
            {"bufferView": 2, "componentType": FLOAT, "count": 4, "type": "VEC3"},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
    }
    if loose_first:
        gltf["meshes"].insert(0, {"primitives": [{"attributes": {"POSITION": 2}}]})
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    chunks = (struct.pack("<II", len(js), 0x4E4F534A) + js
              + struct.pack("<II", len(blob), 0x004E4942) + blob)
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks


class _RecordingGL(types.SimpleNamespace):
    """Stands in for OpenGL.GL, keeping what is uploaded to each target."""

    def __init__(self):
        super().__init__(uploads=[])

    def glBufferData(self, target, size, data, usage):
        self.uploads.append(np.array(data, copy=True))

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 0
        return lambda *a, **k: 1


def _expected_face_normals():
    corners = CUBE[np.array(CUBE_TRIANGLES)]
    normals = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    return normals / np.linalg.norm(normals, axis=1, keepdims=True)


def test_flat_shaded_gives_each_corner_its_triangles_normal():
    positions, normals, uvs, indices = flat_shaded(CUBE, np.array(CUBE_TRIANGLES).reshape(-1))
    assert positions.shape == normals.shape == (36, 3) and uvs is None
    np.testing.assert_array_equal(indices, np.arange(36))
    np.testing.assert_array_equal(positions, CUBE[np.array(CUBE_TRIANGLES).reshape(-1)])
    np.testing.assert_allclose(normals, np.repeat(_expected_face_normals(), 3, axis=0))
    # Six faces, each pointing straight out of the cube.
    axes = {tuple(n) for n in np.round(normals).astype(int).tolist()}
    assert axes == {(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)}


def test_a_degenerate_triangle_keeps_the_default_normal():
    points = np.zeros((3, 3), np.float32)
    _, normals, _, _ = flat_shaded(points)
    np.testing.assert_array_equal(normals, np.tile((0.0, 1.0, 0.0), (3, 1)))


def test_a_glb_without_normals_uploads_flat_normals(tmp_path, monkeypatch):
    path = tmp_path / "cube.glb"
    path.write_bytes(_cube_glb())
    gl = _RecordingGL()
    monkeypatch.setattr(glb_loader, "gl", gl)
    model = GLB(str(path))

    assert model.is_loaded
    vertex_data = gl.uploads[0].reshape(-1, 8)
    assert len(vertex_data) == model.vertex_count == 36
    np.testing.assert_allclose(vertex_data[:, 3:6],
                               np.repeat(_expected_face_normals(), 3, axis=0), atol=1e-6)
    # The CPU copies (2D view, wireframe, collision) are the model's own
    # eight corners and twelve triangles, as before.
    np.testing.assert_array_equal(model.cpu_vertices, CUBE)
    assert model.cpu_triangles == CUBE_TRIANGLES
    assert len(model.get_collision_triangles()) == 12


def test_a_non_indexed_primitive_keeps_its_vertices_in_place(tmp_path, monkeypatch):
    """Unindexed corners are unshared already: the same vertices at the same
    DrawArrays start, each with its triangle's normal."""
    path = tmp_path / "mixed.glb"
    path.write_bytes(_cube_glb(loose_first=True))
    gl = _RecordingGL()
    monkeypatch.setattr(glb_loader, "gl", gl)
    model = GLB(str(path))

    loose, cube = model.groups
    assert loose == {"material": "default", "start": 0, "count": 4,
                     "mode": 4, "indexed": False}
    assert cube == {"material": "default", "start": 0, "count": 36,
                    "mode": 4, "indexed": True}
    vertex_data = gl.uploads[0].reshape(-1, 8)
    np.testing.assert_array_equal(vertex_data[:4, 0:3], LOOSE)
    np.testing.assert_allclose(vertex_data[:3, 3:6], np.tile((0.0, 0.0, 1.0), (3, 1)))
    # The stray vertex, short of a triangle, keeps the default.
    np.testing.assert_array_equal(vertex_data[3, 3:6], (0.0, 1.0, 0.0))
    # The cube's corners follow, and its indices point at them.
    np.testing.assert_array_equal(gl.uploads[1], np.arange(36) + 4)
    np.testing.assert_array_equal(model.cpu_vertices, np.concatenate([LOOSE, CUBE]))
