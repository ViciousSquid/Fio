"""Pure numerical tests for the core portal transform algebra."""

import numpy as np

from engine.portal_transform import (
    basis_from_rotation,
    contains_point,
    corners,
    map_direction,
    map_point,
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
