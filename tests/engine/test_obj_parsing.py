"""OBJ material groups are contiguous, and a bad OBJ cannot hang or crash it.

Regressions covered:

* a material's group started at its first face but counted every face, so an
  OBJ that returned to a material (``usemtl A``, ``B``, ``A`` -- the shipped
  ``Tree low.obj`` does) drew overlapping ranges with the wrong materials;
* one non-numeric coordinate raised ``ValueError`` out of ``load`` (and so
  out of the renderer) instead of costing that one vertex;
* ``mtllib`` was read in full whatever it named, so ``/dev/zero`` never
  returned.
"""

import types

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.obj_loader imports PyOpenGL")

from engine import obj_loader                      # noqa: E402
from engine.obj_loader import OBJ, OBJLoader       # noqa: E402

pytestmark = pytest.mark.qt

INTERLEAVED = """\
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
v 5 5 5
usemtl red
f 1 2 3
usemtl blue
f 1 3 4
usemtl red
f 2 3 4 5
"""


class _NullGL(types.SimpleNamespace):
    """Stands in for OpenGL.GL: every call is a no-op returning a handle."""

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 0
        return lambda *a, **k: 1


@pytest.fixture
def null_gl(monkeypatch):
    monkeypatch.setattr(obj_loader, "gl", _NullGL())


def _write(tmp_path, text, name="model.obj"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_reused_material_is_drawn_as_one_contiguous_group(tmp_path, null_gl):
    model = OBJ(_write(tmp_path, INTERLEAVED))

    assert model.groups == [
        {"material": "red", "start": 0, "count": 9},
        {"material": "blue", "start": 9, "count": 3},
    ]
    # Every red triangle sits inside red's range, in file order.
    red = model.cpu_vertices[0:9].tolist()
    assert red == [[0, 0, 0], [1, 0, 0], [1, 1, 0],
                   [1, 0, 0], [1, 1, 0], [0, 1, 0],
                   [1, 0, 0], [0, 1, 0], [5, 5, 5]]
    assert model.cpu_vertices[9:12].tolist() == [[0, 0, 0], [1, 1, 0], [0, 1, 0]]
    assert model.vertex_count == 12
    assert model.cpu_triangles == [(0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11)]


def test_normals_and_uvs_follow_their_corners(tmp_path, null_gl):
    model = OBJ(_write(tmp_path, "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
                                 "vt 0.25 0.75\nvn 0 0 1\n"
                                 "f 1/1/1 2//1 3/1\n"))
    assert model.is_loaded
    assert model.vertex_count == 3
    assert model.groups == [{"material": "default", "start": 0, "count": 3}]


def test_a_malformed_coordinate_costs_one_vertex(tmp_path):
    loader = OBJLoader()
    assert loader.load(_write(tmp_path, "v 0 0 0\nv 1 nope 0\nv 1 1 0\nvt x y\nvn 0 0 1\n"
                                        "f 1 2 3\n"))
    # The bad vertex keeps its slot, so face indices after it still line up.
    assert loader.vertices == [(0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 0.0)]
    assert loader.texcoords == [(0.0, 0.0)]
    assert loader.normals == [(0.0, 0.0, 1.0)]
    assert [v for v, _, _ in loader.faces[0]["vertices"]] == [0, 1, 2]


def test_a_malformed_material_colour_keeps_the_default(tmp_path):
    _write(tmp_path, "newmtl m\nKd a b c\nKs 1 1 1\nmap_Kd tex.png\n", "model.mtl")
    loader = OBJLoader()
    assert loader.load(_write(tmp_path, "mtllib model.mtl\nv 0 0 0\n"))
    material = loader.materials["m"]
    assert material["diffuse"] == (0.8, 0.8, 0.8)
    assert material["specular"] == (1.0, 1.0, 1.0)
    assert material["texture"] == "tex.png"


def test_mtllib_naming_a_device_is_not_read(tmp_path):
    loader = OBJLoader()
    assert loader.load(_write(tmp_path, "mtllib /dev/zero\nv 0 0 0\n"))
    assert loader.materials == {}


def test_a_special_file_is_not_a_model(tmp_path):
    assert OBJLoader().load("/dev/zero") is False


def test_an_oversized_obj_is_refused_unread(tmp_path, monkeypatch):
    path = _write(tmp_path, INTERLEAVED)
    monkeypatch.setattr(obj_loader, "MAX_OBJ_FILE_BYTES", 16)
    assert OBJLoader().load(path) is False


def test_no_faces_builds_no_buffers(tmp_path, null_gl):
    model = OBJ(_write(tmp_path, "v 0 0 0\n"))
    assert model.vertex_count == 0
    assert model.groups == []
    assert isinstance(model.cpu_vertices, np.ndarray) and len(model.cpu_vertices) == 0
