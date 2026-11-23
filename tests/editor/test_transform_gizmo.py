"""The transform gizmo's geometry and maths (editor.transform_gizmo).

No Qt and no GL: a layout is built from a projection function, so these run
on NumPy + PyGLM alone.
"""

import math

import glm
import numpy as np
import pytest

from editor import transform_gizmo as tg
from engine import brush_geometry
from engine.logic_collision import model_basis

W, H = 800.0, 600.0


def _camera(eye, target=(0.0, 0.0, 0.0)):
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    proj = glm.perspective(glm.radians(70.0), W / H, 0.1, 10000.0)
    pv = proj * view

    def project(p):
        c = pv * glm.vec4(float(p[0]), float(p[1]), float(p[2]), 1.0)
        if c.w <= 1e-6:
            return None
        return ((c.x / c.w * 0.5 + 0.5) * W, (1.0 - (c.y / c.w * 0.5 + 0.5)) * H)

    right, up, _ = tg.camera_axes(view)
    return project, np.array(eye, dtype=float), right, up


def _layout(mode, eye, centre=(0.0, 0.0, 0.0)):
    project, cam, right, up = _camera(eye, centre)
    return tg.layout(mode, centre, project, cam, right, up)


# -- shape --------------------------------------------------------------------

@pytest.mark.parametrize("distance", [150.0, 600.0, 3000.0])
def test_the_gizmo_is_the_same_size_on_screen_at_any_distance(distance):
    eye = np.array((0.6, 0.5, 0.8))
    eye = eye / np.linalg.norm(eye) * distance
    lay = _layout(tg.MOVE, eye)
    cx, cy = lay.centre_px
    # Measured across the screen, where foreshortening does not shorten it.
    project, cam, right, up = _camera(eye)
    edge = project(lay.centre + right * lay.radius_world)
    assert math.hypot(edge[0] - cx, edge[1] - cy) == pytest.approx(tg.RADIUS_PX, rel=0.02)


def test_an_axis_pointing_at_the_camera_fades_and_cannot_be_grabbed():
    lay = _layout(tg.MOVE, (0.0, 0.0, 400.0))         # looking straight down -Z
    assert lay.axes[2][2] == 0.0                       # Z: end-on
    assert lay.axes[0][2] == 1.0 and lay.axes[1][2] == 1.0
    cx, cy = lay.centre_px
    # The XY square faces the camera; the two squares seen edge-on are gone.
    assert set(lay.planes) == {2}
    # Right on the centre is the view-plane circle, never the faded Z arrow.
    assert tg.hit_test(lay, (cx, cy)) == ('move_view',)


def test_move_handles_are_hit_where_they_are_drawn():
    lay = _layout(tg.MOVE, (300.0, 250.0, 350.0))
    for i, (start, tip, alpha) in lay.axes.items():
        mid = ((start[0] + tip[0]) / 2, (start[1] + tip[1]) / 2)
        assert tg.hit_test(lay, mid) == ('move_axis', i)
    for normal, corners in lay.planes.items():
        middle = (sum(c[0] for c in corners) / 4, sum(c[1] for c in corners) / 4)
        assert tg.hit_test(lay, middle) == ('move_plane', normal)
    cx, cy = lay.centre_px
    assert tg.hit_test(lay, (cx + 2 * tg.RADIUS_PX, cy)) is None


def test_rotate_rings_show_only_their_front_half():
    lay = _layout(tg.ROTATE, (300.0, 250.0, 350.0))
    for i, points in lay.rings.items():
        front = sum(1 for _, f in points if f)
        assert 0.4 < front / len(points) < 0.6, (i, front)


def test_rotate_handles_rings_view_ring_and_trackball():
    lay = _layout(tg.ROTATE, (300.0, 250.0, 350.0))
    cx, cy = lay.centre_px
    for i, points in lay.rings.items():
        front = [p for p, f in points if f]
        others = [p for j, pts in lay.rings.items() if j != i for p, f in pts if f]
        # The front point farthest from the other rings (they cross).
        alone = max(front, key=lambda p: min(math.hypot(p[0] - q[0], p[1] - q[1])
                                             for q in others))
        assert tg.hit_test(lay, alone) == ('rotate_axis', i)
    r = tg.VIEW_RING * tg.RADIUS_PX
    assert tg.hit_test(lay, (cx, cy - r)) == ('rotate_view',)
    # Inside the sphere, off the rings: the free trackball.
    assert tg.hit_test(lay, (cx + 4, cy + 3)) in (('rotate_trackball',),
                                                   ('rotate_axis', 0), ('rotate_axis', 1),
                                                   ('rotate_axis', 2))
    assert tg.hit_test(lay, (cx + 0.5 * tg.RADIUS_PX * 0.2, cy - 0.5 * tg.RADIUS_PX)) \
        == ('rotate_trackball',)


def test_scale_handles():
    lay = _layout(tg.SCALE, (300.0, 250.0, 350.0))
    cx, cy = lay.centre_px
    for i, (start, tip, alpha) in lay.axes.items():
        assert tg.hit_test(lay, tip) == ('scale_axis', i)
    r = tg.VIEW_RING * tg.RADIUS_PX
    assert tg.hit_test(lay, (cx - r, cy)) == ('scale_uniform',)


def test_nothing_is_laid_out_behind_the_camera():
    project, cam, right, up = _camera((0.0, 0.0, 100.0), (0.0, 0.0, 200.0))
    assert tg.layout(tg.MOVE, (0.0, 0.0, 0.0), project, cam, right, up) is None


# -- drag maths ---------------------------------------------------------------

def test_ray_axis_param_finds_the_nearest_point_on_the_axis():
    # A ray straight down at x = 40 crosses the X axis at t = 40.
    t = tg.ray_axis_param((40.0, 100.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    assert t == pytest.approx(40.0)
    assert tg.ray_axis_param((0, 0, 50), (1, 0, 0), (0, 0, 0), (1, 0, 0)) is None


def test_ray_plane():
    p = tg.ray_plane((10.0, 50.0, 5.0), (0.0, -1.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    assert p == pytest.approx((10.0, 0.0, 5.0))
    assert tg.ray_plane((0, 50, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0)) is None   # behind
    assert tg.ray_plane((0, 50, 0), (1, 0, 0), (0, 0, 0), (0, 1, 0)) is None   # parallel


def test_signed_angle_is_right_handed():
    assert tg.signed_angle((1, 0, 0), (0, 0, -1), (0, 1, 0)) == pytest.approx(90.0)
    assert tg.signed_angle((1, 0, 0), (0, 0, 1), (0, 1, 0)) == pytest.approx(-90.0)


@pytest.mark.parametrize("axis, angle", [((0, 1, 0), 90.0), ((1, 0, 0), 30.0),
                                         ((0.3, 0.8, -0.5), 137.0)])
def test_rotation_matrix_turns_as_brushes_turn(axis, angle):
    point = [12.0, -7.0, 33.0]
    expected = brush_geometry.rotate_point(point, angle, axis, [0.0, 0.0, 0.0])
    assert tg.rotation_matrix(axis, angle) @ np.array(point) == pytest.approx(expected)


ROTATIONS = [(0, 0, 0), (0, 180, 0), (30, 45, 10), (-60, 270, 5), (89.9, 10, 20),
             (90, 30, 0), (-90, 120, 0), (10, -170, 175)]


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_model_rotation_round_trips_through_its_matrix(rotation):
    m = tg.model_matrix(rotation)
    back = tg.model_rotation_from_matrix(m)
    assert tg.model_matrix(back) == pytest.approx(m, abs=1e-9)


def test_model_matrix_is_the_drawn_model_basis():
    for rotation in ROTATIONS:
        assert tg.model_matrix(rotation) == pytest.approx(
            model_basis(rotation, (1.0, 1.0, 1.0)), abs=1e-12)


@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("axis, angle", [((0, 1, 0), 90.0), ((1, 0, 0), 45.0),
                                         ((0, 0, 1), -30.0), ((0.2, 0.9, 0.4), 70.0)])
def test_turning_a_model_turns_it_about_the_world_axis(rotation, axis, angle):
    turned = tg.turn_model(rotation, axis, angle)
    expected = tg.rotation_matrix(axis, angle) @ model_basis(rotation, (1.0, 1.0, 1.0))
    # Angles are kept to 6 decimal places of a degree.
    assert model_basis(turned, (1.0, 1.0, 1.0)) == pytest.approx(expected, abs=1e-6)


def test_a_yaw_turn_reads_as_a_yaw_change():
    assert tg.turn_model([0.0, 180.0, 0.0], (0, 1, 0), 90.0) == pytest.approx([0.0, -90.0, 0.0])


def test_world_scale_maps_onto_a_turned_models_own_axes():
    # Turned 90 degrees about Y, the model's own X lies along world Z.
    assert tg.local_scale_factors((0, 90, 0), (1.0, 1.0, 2.0)) == pytest.approx([2.0, 1.0, 1.0])
    assert tg.local_scale_factors((0, 0, 0), (3.0, 1.0, 1.0)) == pytest.approx([3.0, 1.0, 1.0])


def test_snap_and_wrap():
    assert tg.snap(23.0, 16.0) == 16.0 and tg.snap(25.0, 16.0) == 32.0
    assert tg.snap(23.0, 0.0) == 23.0
    assert tg.wrap_degrees(190.0) == pytest.approx(-170.0)
    assert tg.wrap_degrees(-190.0) == pytest.approx(170.0)
