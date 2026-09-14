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
