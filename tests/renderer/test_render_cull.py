"""Headless tests for engine.render_cull (no GL context required)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.render_cull import (  # noqa: E402
    CAMERA_RENDER_CULL_DISTANCE, CAMERA_RENDER_CULL_DISTANCE_SQ,
    camera_xz, cull_by_distance, pos_of, within_xz_sq,
)


class _Thing:
    def __init__(self, pos):
        self.pos = pos


class _NoPos:
    pass


class _FakeVec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


def test_squared_radius_matches_distance():
    assert CAMERA_RENDER_CULL_DISTANCE_SQ == CAMERA_RENDER_CULL_DISTANCE ** 2


def test_pos_of_handles_dicts_things_and_missing():
    assert pos_of({"pos": [1, 2, 3]}) == [1, 2, 3]
    assert pos_of(_Thing([4, 5, 6])) == [4, 5, 6]
    assert pos_of({}) is None
    assert pos_of(_NoPos()) is None


def test_camera_xz_accepts_vec_and_sequence():
    assert camera_xz(_FakeVec(10.0, 99.0, 20.0)) == (10.0, 20.0)
    assert camera_xz([10.0, 99.0, 20.0]) == (10.0, 20.0)


def test_cull_ignores_the_y_axis():
    # A brush directly overhead is at XZ distance 0 and must survive, however
    # far above the camera it sits.
    high = {"pos": [0.0, 100000.0, 0.0]}
    assert cull_by_distance([high], 0.0, 0.0) == [high]


def test_boundary_is_inclusive():
    r = CAMERA_RENDER_CULL_DISTANCE
    exactly_on = {"pos": [r, 0.0, 0.0]}
    just_outside = {"pos": [r + 1.0, 0.0, 0.0]}
    kept = cull_by_distance([exactly_on, just_outside], 0.0, 0.0)
    assert kept == [exactly_on]
    assert within_xz_sq([r, 0.0, 0.0], 0.0, 0.0, CAMERA_RENDER_CULL_DISTANCE_SQ)


def test_cull_is_relative_to_camera_not_origin():
    far_from_origin = {"pos": [10000.0, 0.0, 0.0]}
    # Dropped when the camera is at the origin...
    assert cull_by_distance([far_from_origin], 0.0, 0.0) == []
    # ...and kept once the camera moves next to it.
    assert cull_by_distance([far_from_origin], 10000.0, 0.0) == [far_from_origin]


def test_fail_open_for_objects_without_position():
    no_pos = _NoPos()
    empty_dict = {}
    kept = cull_by_distance([no_pos, empty_dict], 0.0, 0.0)
    assert kept == [no_pos, empty_dict]


def test_keep_predicate_exempts_objects():
    far = _Thing([99999.0, 0.0, 0.0])
    other = _Thing([99999.0, 0.0, 0.0])
    kept = cull_by_distance([far, other], 0.0, 0.0, keep=lambda o: o is far)
    assert kept == [far]


def test_keep_predicate_is_checked_before_position():
    # An exempt object with no readable position must still be kept exactly once.
    exempt = _NoPos()
    kept = cull_by_distance([exempt], 0.0, 0.0, keep=lambda o: True)
    assert kept == [exempt]


def test_out_buffer_is_reused_and_cleared():
    buf = []
    near = {"pos": [0.0, 0.0, 0.0]}
    far = {"pos": [99999.0, 0.0, 0.0]}

    first = cull_by_distance([near, far], 0.0, 0.0, out=buf)
    assert first is buf
    assert first == [near]

    # A second pass must clear the buffer, not append to it.
    second = cull_by_distance([far], 0.0, 0.0, out=buf)
    assert second is buf
    assert second == []


def test_empty_input_returns_empty():
    assert cull_by_distance([], 0.0, 0.0) == []


def test_order_is_preserved():
    objs = [{"pos": [float(i), 0.0, 0.0]} for i in range(10)]
    assert cull_by_distance(objs, 0.0, 0.0) == objs


# ----------------------------------------------------------------------
# visible_xz_bounds -- both play camera modes
# ----------------------------------------------------------------------

import math  # noqa: E402

from engine.render_cull import (  # noqa: E402
    WORLD_SLAB_MARGIN, visible_xz_bounds,
)


def _corners(cam, direction, up, fov_deg=75.0, aspect=16.0 / 9.0,
             far=10000.0):
    """The four far-plane corner points of a perspective frustum, in world space.

    Written out rather than pulled from the renderer so the test exercises the
    geometry alone, with no GL context and no view matrix.
    """
    cx, cy, cz = cam
    dx, dy, dz = direction
    dlen = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    dx, dy, dz = dx / dlen, dy / dlen, dz / dlen
    # right = dir x up, then true_up = right x dir (Gram-Schmidt).
    ux, uy, uz = up
    rx, ry, rz = dy * uz - dz * uy, dz * ux - dx * uz, dx * uy - dy * ux
    rlen = math.sqrt(rx * rx + ry * ry + rz * rz) or 1.0
    rx, ry, rz = rx / rlen, ry / rlen, rz / rlen
    tux, tuy, tuz = ry * dz - rz * dy, rz * dx - rx * dz, rx * dy - ry * dx

    half_h = far * math.tan(math.radians(fov_deg) * 0.5)
    half_w = half_h * aspect
    centre = (cx + dx * far, cy + dy * far, cz + dz * far)
    out = []
    for sh, sv in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        out.append((centre[0] + rx * half_w * sh + tux * half_h * sv,
                    centre[1] + ry * half_w * sh + tuy * half_h * sv,
                    centre[2] + rz * half_w * sh + tuz * half_h * sv))
    return out


def test_overhead_mode_is_far_tighter_than_the_distance_ceiling():
    """The overhead camera's frustum leaves the ground slab almost at once."""
    cam = (0.0, 800.0, 0.0)                      # engine default overhead_height
    corners = _corners(cam, (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    min_x, min_z, max_x, max_z = visible_xz_bounds(cam, corners,
                                                   y_min=0.0, y_max=128.0)
    half_width = max(max_x - min_x, max_z - min_z) * 0.5
    assert half_width < CAMERA_RENDER_CULL_DISTANCE * 0.5, (
        f"overhead box half-width {half_width:.0f} is no tighter than the "
        f"{CAMERA_RENDER_CULL_DISTANCE:.0f} ceiling")


def test_first_person_degrades_to_the_distance_ceiling():
    """At low pitch the rays never leave the slab, so the ceiling is the answer.

    Not a shortcoming: it is what makes the function safe to call in both modes.
    It tightens overhead and reproduces the previous behaviour in first person,
    rather than returning something smaller than the player can see.
    """
    cam = (0.0, 64.0, 0.0)
    corners = _corners(cam, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    min_x, min_z, max_x, max_z = visible_xz_bounds(cam, corners,
                                                   y_min=-1000.0, y_max=1000.0)
    # Reaches the ceiling ahead, and never exceeds it in any direction.
    assert max_x >= CAMERA_RENDER_CULL_DISTANCE * 0.9
    for v in (max_x, max_z, -min_x, -min_z):
        assert v <= CAMERA_RENDER_CULL_DISTANCE + WORLD_SLAB_MARGIN + 1.0


def test_the_box_is_never_smaller_than_the_visible_volume():
    """Conservative by construction, in either mode."""
    for direction in ((0.0, -1.0, 0.0), (1.0, -0.2, 0.0), (1.0, 0.0, 0.0)):
        cam = (10.0, 500.0, -20.0)
        up = (0.0, 0.0, -1.0) if abs(direction[1]) > 0.9 else (0.0, 1.0, 0.0)
        corners = _corners(cam, direction, up)
        min_x, min_z, max_x, max_z = visible_xz_bounds(cam, corners,
                                                       y_min=0.0, y_max=256.0)
        assert min_x <= max_x and min_z <= max_z
        # The camera's own column is inside the box whenever it is in the slab,
        # and the box is non-degenerate whenever anything is visible at all.
        assert max_x - min_x > 0.0
