"""OBJ faces are parsed into packed arrays, and build exactly what they did.

Regression covered: every face was a dict holding a list of ``(v, vt, vn)``
tuples -- about 520 bytes for a triangle -- so an 8 MB OBJ of ``f 1 2 3``
lines peaked at 521 MB and one at the (then 16 M) triangle cap at about
8 GB. The faces are now flat int arrays; ``OBJLoader.faces`` still reads as
the same dicts, and the vertex buffer built from them is unchanged.
"""

import random
import tracemalloc
import types

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.obj_loader imports PyOpenGL")

from engine import obj_loader                      # noqa: E402
from engine.obj_loader import OBJ, OBJLoader       # noqa: E402

pytestmark = pytest.mark.qt


class _RecordingGL(types.SimpleNamespace):
    """Stands in for OpenGL.GL, keeping what glBufferData was given."""

    def __getattr__(self, name):
        if name.startswith("GL_"):
            return 0
        if name == "glBufferData":
            return lambda _target, _size, data, _usage: self.uploads.append(np.array(data))
        return lambda *a, **k: 1


@pytest.fixture
def gl(monkeypatch):
    fake = _RecordingGL(uploads=[])
    monkeypatch.setattr(obj_loader, "gl", fake)
    return fake


def _random_obj(rng):
    nv, nt, nn = rng.randint(3, 30), rng.randint(0, 10), rng.randint(0, 10)
    lines = [f"v {rng.random()} {rng.random()} {rng.random()}" for _ in range(nv)]
    lines += [f"vt {rng.random()} {rng.random()}" for _ in range(nt)]
    lines += [f"vn {rng.random()} {rng.random()} {rng.random()}" for _ in range(nn)]
    for _ in range(rng.randint(0, 40)):
        if rng.random() < 0.15:
            lines.append("usemtl " + rng.choice(["a", "b", "c d", "e"]))
            continue
        corners = []
        for _ in range(rng.randint(3, 7)):
            v = rng.randint(-nv - 2, nv + 2)
            t, n = rng.randint(-nt - 1, nt + 1), rng.randint(-nn - 1, nn + 1)
            corners.append(rng.choice([f"{v}", f"{v}/{t}", f"{v}//{n}", f"{v}/{t}/{n}"]))
        lines.append("f " + " ".join(corners))
    return "\n".join(lines) + "\n"


def _reference_build(loader):
    """The vertex data and groups the per-face-dict build produced, from the
    face dicts: fan each face, group by material in order of first use."""
    corners, tri_material, rank = [], [], {}
    for face in loader.faces:
        r = rank.setdefault(face.get("material"), len(rank))
        verts = face["vertices"]
        for i in range(1, len(verts) - 1):
            corners += [verts[0], verts[i], verts[i + 1]]
            tri_material.append(r)
    corner_idx = np.asarray(corners, dtype=np.int64).reshape(-1, 3)
    tri_material = np.asarray(tri_material, dtype=np.int64)
    order = np.argsort(tri_material, kind="stable")
    corner_idx = corner_idx.reshape(-1, 3, 3)[order].reshape(-1, 3)
    n = len(corner_idx)
    data = np.zeros((n, 8), dtype=np.float32)
    data[:, 4] = 1.0
    for slot, source, column, width in ((0, loader.vertices, 0, 3),
                                        (2, loader.normals, 3, 3),
                                        (1, loader.texcoords, 6, 2)):
        if n == 0 or not source:
            continue
        table = np.asarray(source, dtype=np.float32).reshape(-1, width)
        idx = corner_idx[:, slot]
        valid = (idx >= 0) & (idx < len(table))
        data[valid, column:column + width] = table[idx[valid]]
    counts = np.bincount(tri_material, minlength=len(rank)) * 3
    groups, start = [], 0
    for name, r in rank.items():
        if counts[r] > 0:
            groups.append({"material": name or "default", "start": start,
                           "count": int(counts[r])})
        start += int(counts[r])
    return data.reshape(-1), groups


def test_faces_read_as_the_dicts_they_were(tmp_path):
    loader = OBJLoader()
    assert loader.load_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nvt 0 0\n"
                            "f 1/1 2 3\nusemtl red\nf 1 3 4 -1\n", "m.obj")
    assert len(loader.faces) == 2
    assert loader.faces == [
        {"vertices": [(0, 0, -1), (1, -1, -1), (2, -1, -1)], "material": None},
        {"vertices": [(0, -1, -1), (2, -1, -1), (3, -1, -1), (3, -1, -1)],
         "material": "red"},
    ]
    assert loader.faces[-1]["material"] == "red"
    assert [f["material"] for f in loader.faces[0:2]] == [None, "red"]
    assert loader.material_names() == [None, "red"]


def test_material_order_is_first_use_by_a_face_not_first_usemtl():
    loader = OBJLoader()
    assert loader.load_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl A\nusemtl B\n"
                            "f 1 2 3\nusemtl A\nf 1 2 3\n", "m.obj")
    assert loader.material_names() == ["B", "A"]


def test_random_models_build_exactly_what_the_face_dicts_built(tmp_path, gl):
    rng = random.Random(7)
    for trial in range(150):
        path = tmp_path / f"m{trial}.obj"
        path.write_text(_random_obj(rng), encoding="utf-8")
        loader = OBJLoader()
        assert loader.load(str(path))
        expected_data, expected_groups = _reference_build(loader)
        gl.uploads.clear()
        model = OBJ(str(path))
        assert model.groups == expected_groups
        assert model.vertex_count * 8 == len(expected_data)
        if model.vertex_count:
            assert np.array_equal(gl.uploads[-1], expected_data)
            assert model.cpu_triangles == [(i, i + 1, i + 2)
                                           for i in range(0, model.vertex_count, 3)]


def test_faces_assigned_as_dicts_are_packed_too():
    loader = OBJLoader()
    loader.faces = [{"vertices": [(0, -1, -1), (1, -1, -1), (2, -1, -1)], "material": "x"}]
    loader.faces.append({"vertices": [(2, 0, 0), (1, 0, 0), (0, 0, 0)]})
    assert [f["material"] for f in loader.faces] == ["x", None]
    corners, materials = loader._faces.triangles()
    assert corners.shape == (2, 3, 3) and list(materials) == [0, 1]


def test_a_triangle_costs_tens_of_bytes_not_hundreds():
    count = 20_000
    text = "v 0 0 0\nv 1 0 0\nv 0 1 0\n" + "f 1 2 3\n" * count
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        loader = OBJLoader()
        assert loader.load_text(text, "many.obj")
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert len(loader.faces) == count
    # 9 ints of corners, a start and a material id: 44 bytes (the dict form
    # held ~520). Array growth slack allowed for.
    assert held / count < 80


def test_the_triangle_cap_fits_what_the_layout_can_hold():
    """At the cap, the packed faces (44 bytes a triangle) and the vertex
    buffer (96 bytes a triangle) stay under 1 GB together."""
    assert obj_loader.MAX_OBJ_TRIANGLES * (44 + 96) < 1024 ** 3
