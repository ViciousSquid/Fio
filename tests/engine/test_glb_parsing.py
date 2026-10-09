"""GLB accessors are decoded in bulk, and a hostile GLB cannot misdirect it.

Every case builds a real ``.glb`` byte-for-byte, so the loader is exercised
through ``load`` exactly as the renderer, the collision builder and the asset
browser call it.

Regressions covered:

* index accessors came back as ``(n, 1)`` arrays and ``GLB`` converted each
  row with ``int()``, which NumPy 2 rejects -- every indexed GLB raised
  ``TypeError`` out of the constructor into the renderer;
* an index past the vertex data was uploaded as-is, so the GPU read outside
  the vertex buffer;
* an external buffer ``uri`` was joined unchecked, so ``/dev/zero`` (or any
  absolute path) was read in full.
"""

import json
import struct
import types

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.glb_loader imports PyOpenGL")

from engine import glb_loader                      # noqa: E402
from engine.glb_loader import GLB, GLBLoader       # noqa: E402

pytestmark = pytest.mark.qt

FLOAT, USHORT, UINT = 5126, 5123, 5125


def _glb(gltf, blob=b""):
    """Pack a glTF dict and a BIN payload into GLB container bytes."""
    js = json.dumps(gltf).encode()
    js += b" " * (-len(js) % 4)
    blob += b"\0" * (-len(blob) % 4)
    chunks = struct.pack("<II", len(js), 0x4E4F534A) + js
    if blob:
        chunks += struct.pack("<II", len(blob), 0x004E4942) + blob
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks


def _quad(indices=(0, 1, 2, 0, 2, 3), index_type=USHORT, stride=32, buffers=None):
    """Two triangles with interleaved POSITION/NORMAL/TEXCOORD_0 and indices."""
    positions = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.float32)
    normals = np.tile(np.array([0, 0, 1], np.float32), (4, 1))
    uvs = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)

    vertex = bytearray(stride * 4)
    for i in range(4):
        struct.pack_into("<8f", vertex, i * stride, *positions[i], *normals[i], *uvs[i])
    index_fmt = "<H" if index_type == USHORT else "<I"
    index = b"".join(struct.pack(index_fmt, i) for i in indices)
    index += b"\0" * (-len(index) % 4)
    blob = bytes(vertex) + index

    gltf = {
        "asset": {"version": "2.0"},
        "buffers": buffers if buffers is not None else [{"byteLength": len(blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vertex), "byteStride": stride},
            {"buffer": 0, "byteOffset": len(vertex), "byteLength": len(index)},
        ],
        "accessors": [
            {"bufferView": 0, "byteOffset": 0, "componentType": FLOAT, "count": 4, "type": "VEC3"},
            {"bufferView": 0, "byteOffset": 12, "componentType": FLOAT, "count": 4, "type": "VEC3"},
            {"bufferView": 0, "byteOffset": 24, "componentType": FLOAT, "count": 4, "type": "VEC2"},
            {"bufferView": 1, "componentType": index_type, "count": len(indices), "type": "SCALAR"},
        ],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
            "indices": 3,
        }]}],
    }
    return gltf, blob, positions, normals, uvs


def _load(tmp_path, gltf, blob=b""):
    path = tmp_path / "model.glb"
    path.write_bytes(_glb(gltf, blob))
    loader = GLBLoader()
    loader._filepath_hint = str(path)
    assert loader.load(str(path))
    return loader


class _NullGL(types.SimpleNamespace):
    """Stands in for OpenGL.GL: every call is a no-op returning a handle."""

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 0
        return lambda *a, **k: 1


def _model(tmp_path, monkeypatch, gltf, blob=b""):
    path = tmp_path / "model.glb"
    path.write_bytes(_glb(gltf, blob))
    monkeypatch.setattr(glb_loader, "gl", _NullGL())
    return GLB(str(path))


def test_interleaved_accessors_decode_exactly(tmp_path):
    gltf, blob, positions, normals, uvs = _quad()
    prim = _load(tmp_path, gltf, blob).meshes[0]["primitives"][0]

    np.testing.assert_array_equal(prim["positions"], positions)
    np.testing.assert_array_equal(prim["normals"], normals)
    np.testing.assert_array_equal(prim["uvs"], uvs)
    assert prim["positions"].dtype == np.float32
    assert prim["indices"].tolist() == [0, 1, 2, 0, 2, 3]


def test_an_indexed_glb_builds_its_buffers(tmp_path, monkeypatch):
    gltf, blob, positions, normals, uvs = _quad(index_type=UINT)
    model = _model(tmp_path, monkeypatch, gltf, blob)

    assert model.is_loaded
    assert model.vertex_count == 4 and model.index_count == 6
    assert model.has_indices
    np.testing.assert_array_equal(model.cpu_vertices, positions)
    assert model.cpu_triangles == [(0, 1, 2), (0, 2, 3)]
    assert model.groups == [{"material": "default", "start": 0, "count": 6,
                             "mode": 4, "indexed": True}]


def test_an_accessor_running_past_its_buffer_is_truncated(tmp_path):
    gltf, blob, positions, _, _ = _quad()
    gltf["accessors"][0]["count"] = 10_000_000
    prim = _load(tmp_path, gltf, blob).meshes[0]["primitives"][0]
    # Only whole elements inside the blob are read (the index data after the
    # vertices still decodes as a few extra positions, as before).
    assert len(prim["positions"]) < 10
    np.testing.assert_array_equal(prim["positions"][:4], positions)


def test_out_of_range_indices_drop_the_primitive(tmp_path, monkeypatch):
    gltf, blob, *_ = _quad(indices=(0, 1, 2, 0, 2, 4000))
    assert _load(tmp_path, gltf, blob).meshes[0]["primitives"] == []
    model = _model(tmp_path, monkeypatch, gltf, blob)
    assert model.vertex_count == 0 and model.index_count == 0


def test_a_huge_unsigned_index_does_not_wrap_into_range(tmp_path):
    gltf, blob, *_ = _quad(indices=(0, 1, 2, 0, 2, 0xFFFFFFFF), index_type=UINT)
    assert _load(tmp_path, gltf, blob).meshes[0]["primitives"] == []


def test_negative_offsets_are_rejected(tmp_path):
    gltf, blob, *_ = _quad()
    gltf["accessors"][0]["byteOffset"] = -12
    assert _load(tmp_path, gltf, blob).meshes[0]["primitives"] == []


def test_a_partial_trailing_triangle_is_ignored(tmp_path):
    gltf, blob, *_ = _quad(indices=(0, 1, 2, 0, 2))
    assert _load(tmp_path, gltf, blob).get_flattened_triangles() == [(0, 1, 2)]


@pytest.mark.parametrize("uri", ["/dev/zero", "/etc/hostname", "file:///etc/hostname",
                                 "C:/Windows/win.ini"])
def test_external_buffers_must_be_relative_regular_files(tmp_path, uri):
    gltf, blob, *_ = _quad(buffers=[{"uri": uri, "byteLength": 1024}])
    loader = _load(tmp_path, gltf)
    assert loader.buffers == [b""]
    assert loader.meshes[0]["primitives"] == []


def test_a_relative_external_buffer_still_loads(tmp_path):
    gltf, blob, positions, _, _ = _quad(buffers=[{"uri": "model.bin", "byteLength": 0}])
    (tmp_path / "model.bin").write_bytes(blob)
    prim = _load(tmp_path, gltf).meshes[0]["primitives"][0]
    np.testing.assert_array_equal(prim["positions"], positions)


def test_an_oversized_glb_is_refused_unread(tmp_path, monkeypatch):
    gltf, blob, *_ = _quad()
    path = tmp_path / "model.glb"
    path.write_bytes(_glb(gltf, blob))
    monkeypatch.setattr(glb_loader, "MAX_GLB_FILE_BYTES", 64)
    assert GLBLoader().load(str(path)) is False
