"""Model Prop collision follows the model's rotation exactly as it is drawn.

Collision once rotated yaw the opposite way from the renderer, and applied
yaw/pitch/roll in the reverse order. Yaw 0 and 180 agree either way, so it
only showed on models turned 90 or 270 degrees: a staircase drawn climbing one
way collided climbing the other.
"""
import math

import numpy as np
import pytest

from editor.editor_state import EditorState
from engine.entity_table import _model_transform_columns
from engine.logic_collision import model_basis, model_world_box
from engine.logic_thread import LogicThread
from engine.prop_entity import Prop
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt

# A long, lopsided wedge: every axis has a different extent and the model is
# off-centre, so any rotation or ordering mistake moves at least one vertex.
_VERTICES = (
    (0.0, 0.0, 0.0), (320.0, 0.0, 0.0), (320.0, 160.0, 0.0),
    (0.0, 0.0, 48.0), (320.0, 0.0, 48.0), (320.0, 160.0, 48.0),
)
_FACES = ((1, 2, 3), (4, 6, 5), (1, 4, 5, 2), (2, 5, 6, 3), (1, 3, 6, 4))


def _write_obj(path):
    lines = ["v %g %g %g" % v for v in _VERTICES]
    lines += ["f " + " ".join(str(i) for i in face) for face in _FACES]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _renderer_basis(rotation, scale):
    prop = Prop(pos=[0.0, 0.0, 0.0], properties={
        'render_mode': 'model', 'model_path': 'x.obj',
        'rotation': list(rotation), 'scale': list(scale),
    })
    model, _normal = _model_transform_columns(prop)
    # Column-major 4x4 -> the upper-left 3x3 as a row-major matrix.
    return np.asarray(model, dtype=np.float64).reshape(4, 4).T[:3, :3]


def _runtime(prop):
    state = EditorState()
    state.brushes = []
    state.things = [prop]
    return LogicThread(ThreadedGameState(), state).collision_runtime


@pytest.mark.parametrize("rotation", [
    (0.0, 0.0, 0.0), (0.0, 90.0, 0.0), (0.0, 270.0, 0.0), (0.0, 33.0, 0.0),
    (25.0, 0.0, 0.0), (0.0, 0.0, 40.0), (20.0, 270.0, -15.0), (-70.0, 135.0, 10.0),
])
def test_collision_basis_is_the_renderer_model_matrix(rotation):
    scale = (1.5, 0.75, 2.0)
    assert np.allclose(model_basis(rotation, scale),
                       _renderer_basis(rotation, scale), atol=1e-5)


@pytest.mark.parametrize("yaw", [90.0, 180.0, 270.0])
def test_mesh_collision_vertices_sit_where_the_model_is_drawn(tmp_path, yaw):
    model_path = _write_obj(tmp_path / "wedge.obj")
    pos = [100.0, 8.0, -40.0]
    rotation = [0.0, yaw, 0.0]
    scale = [1.0, 1.0, 1.0]
    prop = Prop(pos=pos, properties={
        'render_mode': 'model', 'model_path': model_path,
        'rotation': rotation, 'scale': scale, 'collision_shape': 'mesh',
    })
    triangles = _runtime(prop).compute_model_collision_mesh(
        model_path, pos, scale, rotation)
    assert triangles

    drawn = (np.asarray(_VERTICES) @ _renderer_basis(rotation, scale).T
             + np.asarray(pos))
    collided = np.asarray([v for tri, _n in triangles for v in tri])
    for vertex in collided:
        assert np.min(np.linalg.norm(drawn - vertex, axis=1)) < 1e-3

    # The 320-unit run, turned a quarter, must run along Z rather than X.
    extent = collided.max(axis=0) - collided.min(axis=0)
    if yaw in (90.0, 270.0):
        assert extent[2] == pytest.approx(320.0, abs=1e-3)
        assert extent[0] == pytest.approx(48.0, abs=1e-3)
    # The high end of the wedge is where the renderer puts it.
    top = collided[np.argmax(collided[:, 1])]
    expected_top = drawn[np.argmax(drawn[:, 1])]
    assert top[0] == pytest.approx(expected_top[0], abs=1e-3)
    assert top[2] == pytest.approx(expected_top[2], abs=1e-3)


def test_yaw_270_climbs_toward_plus_z():
    """Pin the convention itself: +X model space turned 270 points along +Z."""
    forward = model_basis((0.0, 270.0, 0.0), (1.0, 1.0, 1.0)) @ (1.0, 0.0, 0.0)
    assert np.allclose(forward, (0.0, 0.0, 1.0), atol=1e-9)


def test_aabb_collision_box_encloses_the_rotated_model(tmp_path):
    model_path = _write_obj(tmp_path / "wedge.obj")
    pos = [10.0, 0.0, 20.0]
    prop = Prop(pos=pos, properties={
        'render_mode': 'model', 'model_path': model_path,
        'rotation': [0.0, 270.0, 0.0], 'scale': [1.0, 1.0, 1.0],
        'collision_shape': 'aabb', 'collision_size': [0.0, 0.0, 0.0],
        'physics_enabled': False, 'no_collision': False,
    })
    runtime = _runtime(prop)
    brushes = runtime.build_model_collision_brushes()
    assert len(brushes) == 1
    box = brushes[0]
    assert box['_collision_mode'] == 'aabb'
    assert box['size'] == pytest.approx([48.0, 160.0, 320.0], abs=1e-3)
    # Model-space centre (160, 80, 24) turned 270 -> (-24, 80, 160).
    assert box['pos'] == pytest.approx([-14.0, 80.0, 180.0], abs=1e-3)


def test_world_box_of_a_diagonal_model_grows_to_enclose_it():
    centre, size = model_world_box(((-10.0, 0.0, -10.0), (10.0, 4.0, 10.0)),
                                   (0.0, 0.0, 0.0), (0.0, 45.0, 0.0),
                                   (1.0, 1.0, 1.0))
    assert centre == pytest.approx([0.0, 2.0, 0.0])
    assert size == pytest.approx([20.0 * math.sqrt(2.0), 4.0, 20.0 * math.sqrt(2.0)])


@pytest.mark.parametrize("yaw", [90.0, 270.0])
def test_bundled_catwalk_stairs_collide_as_drawn(yaw):
    """The shipped staircase (bundled_catwalks.fiopak), turned a quarter."""
    from engine.asset_archives import asset_exists
    path = 'catwalk_straight_stairs_320.obj'
    if not asset_exists('assets/models/' + path):
        pytest.skip("bundled catwalk pack not present")
    scale = [1.0, 1.0, 1.0]
    origin = [0.0, 0.0, 0.0]
    runtime = _runtime(Prop(pos=origin, properties={
        'render_mode': 'model', 'model_path': path, 'collision_shape': 'mesh',
    }))
    unturned = runtime.compute_model_collision_mesh(
        path, origin, scale, [0.0, 0.0, 0.0])
    turned = runtime.compute_model_collision_mesh(
        path, origin, scale, [0.0, yaw, 0.0])
    assert unturned and turned and len(unturned) == len(turned)

    model_space = np.asarray([v for tri, _n in unturned for v in tri])
    drawn = model_space @ _renderer_basis((0.0, yaw, 0.0), scale).T
    collided = np.asarray([v for tri, _n in turned for v in tri])
    assert np.allclose(collided, drawn, atol=1e-3)
