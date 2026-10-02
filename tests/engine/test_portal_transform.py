"""Pure numerical tests for the core portal transform algebra."""

import numpy as np

from engine.portal_transform import (
    basis_from_rotation,
    contains_point,
    corners,
    map_direction,
    map_point,
    mirror_point,
)


def test_basis_is_orthonormal_for_tilted_portals():
    basis = np.asarray(basis_from_rotation([37.0, -23.0, 11.0]))
    assert np.allclose(basis @ basis.T, np.eye(3), atol=1e-12)


def test_point_and_direction_mapping_use_one_shared_transform():
    a_pos = (10.0, 20.0, 30.0)
    b_pos = (-80.0, 12.0, 140.0)
    a_basis = basis_from_rotation([30.0, 15.0, 5.0])
    b_basis = basis_from_rotation([-70.0, -10.0, 20.0])
    p = (25.0, 60.0, -12.0)
    d = (0.3, -0.4, 0.5)

    mapped = map_point(a_pos, a_basis, b_pos, b_basis, p)
    round_trip = map_point(b_pos, b_basis, a_pos, a_basis, mapped)
    assert np.allclose(round_trip, p, atol=1e-9)

    mapped_dir = map_direction(a_basis, b_basis, d)
    round_trip_dir = map_direction(b_basis, a_basis, mapped_dir)
    assert np.allclose(round_trip_dir, d, atol=1e-9)


def test_aperture_corners_and_contains_share_the_same_frame():
    pos = (4.0, 8.0, 12.0)
    basis = basis_from_rotation([90.0, 0.0, 0.0])
    quad = corners(pos, basis, 128.0, 256.0)

    assert len(quad) == 4
    assert all(contains_point(pos, basis, 128.0, 256.0, p) for p in quad)
    assert contains_point(pos, basis, 128.0, 256.0, pos)
    far = (pos[0] + 1000.0, pos[1], pos[2])
    assert not contains_point(pos, basis, 128.0, 256.0, far)


def test_mirror_point_shows_the_viewer_straight_back_through_the_portal():
    """A portal used as a mirror: the reflection sits in front of the exit at
    the viewer's own distance, dead ahead of the virtual camera at 2x range."""
    a_pos = (1072.0, 224.0, -800.0)
    b_pos = (480.0, 224.0, -448.0)
    a_basis = basis_from_rotation([180.0, 0.0, 0.0])
    b_basis = basis_from_rotation([270.0, 0.0, 0.0])
    eye = (1000.0, 154.0, -1100.0)

    cam = np.asarray(map_point(a_pos, a_basis, b_pos, b_basis, eye))
    mirrored = np.asarray(mirror_point(a_pos, a_basis, b_pos, b_basis, eye))

    a_n = np.asarray(a_basis[2])
    b_n = np.asarray(b_basis[2])
    dist = float(np.dot(np.asarray(eye) - a_pos, a_n))
    assert dist > 0.0
    assert np.isclose(np.dot(mirrored - b_pos, b_n), dist)
    assert np.isclose(np.dot(cam - b_pos, b_n), -dist)
    # Looking straight into the source portal means looking along -normal,
    # which maps to +normal at the exit: the reflection is right ahead.
    ahead = np.asarray(map_direction(a_basis, b_basis, tuple(-a_n)))
    assert np.allclose(mirrored - cam, ahead * 2.0 * dist, atol=1e-9)
