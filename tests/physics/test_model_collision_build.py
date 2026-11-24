"""Building model collision: one read of a file, triangles kept while a model
stays put.

Every solid model Prop used to re-read and re-parse its model file, and build
its world triangles and normals one by one in Python: forty chairs placed
from the Asset Browser made Play start take two seconds, and any entity
coming or going mid-Play (a barrel exploding, a plugin's spawn) rebuilt all
of it -- a 1.7 s freeze on a 0.14 ms tick.
"""

import math

import numpy as np
import pytest

from editor.editor_state import EditorState
from engine import obj_loader
from engine.logic_collision import model_basis
from engine.logic_thread import LogicThread
from engine.prop_entity import Prop
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt

_VERTICES = (
    (0.0, 0.0, 0.0), (320.0, 0.0, 0.0), (320.0, 160.0, 0.0),
    (0.0, 0.0, 48.0), (320.0, 0.0, 48.0), (320.0, 160.0, 48.0),
)
# The last face is a sliver of no area, and the one before names a vertex
# the file does not have: neither is a surface.
_FACES = ((1, 2, 3), (4, 6, 5), (1, 4, 5, 2), (2, 5, 6, 3), (1, 3, 6, 4), (1, 2, 2))


def _write_obj(path):
    lines = ["v %r %r %r" % v for v in _VERTICES]
    lines += ["f " + " ".join(str(i) for i in face) for face in _FACES]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _prop(model_path, pos, rotation=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), **more):
    properties = {'render_mode': 'model', 'model_path': model_path,
                  'rotation': list(rotation), 'scale': list(scale),
                  'collision_shape': 'mesh', 'collision_size': [0.0, 0.0, 0.0],
                  'physics_enabled': False, 'no_collision': False}
    properties.update(more)
    return Prop(pos=list(pos), properties=properties)


def _logic(*props):
    state = EditorState()
    state.brushes = []
    state.things = list(props)
    return LogicThread(ThreadedGameState(), state)


def _reference(pos, rotation, scale):
    """The triangles and normals worked out one at a time, as they were."""
    basis = model_basis(rotation, scale)
    world = [tuple(map(float, np.asarray(v, dtype=np.float64) @ basis.T
                       + np.asarray(pos, dtype=np.float64))) for v in _VERTICES]
    out = []
    for face in _FACES:
        corners = [i - 1 for i in face]
        for k in range(1, len(corners) - 1):
            w0, w1, w2 = world[corners[0]], world[corners[k]], world[corners[k + 1]]
            e1 = (w1[0] - w0[0], w1[1] - w0[1], w1[2] - w0[2])
            e2 = (w2[0] - w0[0], w2[1] - w0[1], w2[2] - w0[2])
            nx = e1[1] * e2[2] - e1[2] * e2[1]
            ny = e1[2] * e2[0] - e1[0] * e2[2]
            nz = e1[0] * e2[1] - e1[1] * e2[0]
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if not math.isfinite(length) or length <= 1e-8:
                continue
            out.append(((w0, w1, w2), (nx / length, ny / length, nz / length)))
    return out


@pytest.mark.parametrize("rotation, scale", [
    ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
    ((0.0, 90.0, 0.0), (1.0, 1.0, 1.0)),
    ((20.0, 270.0, -15.0), (1.5, 0.75, 2.0)),
    ((-70.0, 135.0, 10.0), (0.01, 0.01, 0.01)),
])
def test_the_triangles_and_normals_are_the_ones_built_one_at_a_time(tmp_path, rotation, scale):
    model_path = _write_obj(tmp_path / "wedge.obj")
    pos = [100.0, 8.0, -40.0]
    runtime = _logic(_prop(model_path, pos, rotation, scale)).collision_runtime
    built = runtime.compute_model_collision_mesh(model_path, pos, list(scale), list(rotation))
    reference = _reference(pos, rotation, scale)
    assert len(built) == len(reference) == 8, "the sliver is left out"
    for (triangle, normal), (wanted, wanted_normal) in zip(built, reference):
        assert [tuple(v) for v in triangle] == list(wanted)        # to the bit
        assert tuple(normal) == wanted_normal


def test_a_mesh_proxy_carries_its_bounds_and_arrays(tmp_path):
    model_path = _write_obj(tmp_path / "wedge.obj")
    runtime = _logic(_prop(model_path, [10.0, 0.0, 0.0])).collision_runtime
    (brush,) = runtime.build_model_collision_brushes()
    triangles = brush['_mesh_triangles']
    points = np.asarray([tri for tri, _n in triangles]).reshape(-1, 3)
    assert brush['_mesh_bounds'] == (points.min(axis=0).tolist(), points.max(axis=0).tolist())
    kept, corners, normals = brush['_mesh_arrays']
    assert kept is triangles
    assert corners.tolist() == [list(map(list, tri)) for tri, _n in triangles]
    assert normals.tolist() == [list(n) for _tri, n in triangles]


def _count_reads(monkeypatch):
    reads = []
    real = obj_loader.OBJLoader.load

    def load(self, path, *args, **kwargs):
        reads.append(path)
        return real(self, path, *args, **kwargs)
    monkeypatch.setattr(obj_loader.OBJLoader, "load", load)
    return reads


def test_a_model_file_is_read_once_for_all_the_props_that_use_it(tmp_path, monkeypatch):
    model_path = _write_obj(tmp_path / "wedge.obj")
    props = [_prop(model_path, [i * 400.0, 0.0, 0.0], (0.0, i * 17.0, 0.0)) for i in range(12)]
    runtime = _logic(*props).collision_runtime
    reads = _count_reads(monkeypatch)
    brushes = runtime.build_model_collision_brushes()
    assert len(brushes) == 12 and len(reads) == 1
    assert len({id(b['_mesh_triangles']) for b in brushes}) == 12, "each its own place"


def test_outside_play_each_build_reads_the_file_again(tmp_path, monkeypatch):
    """The editor: the file may have been changed between two builds."""
    model_path = _write_obj(tmp_path / "wedge.obj")
    runtime = _logic(_prop(model_path, [0.0, 0.0, 0.0])).collision_runtime
    reads = _count_reads(monkeypatch)
    first = runtime.build_model_collision_brushes()[0]['_mesh_triangles']
    second = runtime.build_model_collision_brushes()[0]['_mesh_triangles']
    assert len(reads) == 2 and first is not second and first == second


def test_in_play_a_rebuild_keeps_the_triangles_of_what_has_not_moved(tmp_path, monkeypatch):
    model_path = _write_obj(tmp_path / "wedge.obj")
    still = _prop(model_path, [0.0, 0.0, 0.0])
    moved = _prop(model_path, [900.0, 0.0, 0.0])
    logic = _logic(still, moved)
    runtime = logic.collision_runtime
    logic.session_runtime.play_mode = True
    runtime.forget_model_files()                    # as Play start does
    reads = _count_reads(monkeypatch)
    try:
        before = {id(b['_physics_entity']): b for b in runtime.build_model_collision_brushes()}
        moved.pos = [900.0, 64.0, 0.0]
        after = {id(b['_physics_entity']): b for b in runtime.build_model_collision_brushes()}
    finally:
        logic.session_runtime.play_mode = False
    assert len(reads) == 1, "one read for the session"
    assert after[id(still)] is not before[id(still)], "a new proxy each build"
    assert after[id(still)]['_mesh_triangles'] is before[id(still)]['_mesh_triangles']
    assert after[id(still)]['_mesh_arrays'][0] is after[id(still)]['_mesh_triangles']
    new, old = after[id(moved)]['_mesh_triangles'], before[id(moved)]['_mesh_triangles']
    assert new is not old
    assert new[0][0][0][1] == pytest.approx(old[0][0][0][1] + 64.0)
    assert after[id(moved)]['_mesh_bounds'][0][1] == pytest.approx(64.0)

    runtime.forget_model_files()                    # as leaving Play does
    assert runtime.build_model_collision_brushes()[0]['_mesh_triangles'] is not \
        after[id(still)]['_mesh_triangles']
