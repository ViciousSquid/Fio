"""The decal table: a ring of projected-decal boxes (engine/decal_table.py)."""

import numpy as np
import pytest

from engine.decal_table import FLOOR, WALL, DecalTable, box_matrix


def _matrix(values):
    return np.asarray(values, dtype=np.float64).reshape(4, 4).T     # column-major


def test_a_box_matrix_takes_the_unit_cube_to_the_box():
    m = _matrix(box_matrix((10.0, 20.0, 30.0), (0.0, 0.0, 1.0), 40.0, 8.0, height=60.0))
    assert np.allclose(m[:3, 3], (10.0, 20.0, 30.0))
    right, up, forward = m[:3, 0], m[:3, 1], m[:3, 2]
    assert np.allclose(np.linalg.norm(right), 40.0) and np.allclose(np.linalg.norm(up), 60.0)
    assert np.allclose(forward, (0.0, 0.0, 8.0))
    assert np.allclose(up / 60.0, (0.0, 1.0, 0.0))          # level throw: up is up
    # A proper rotation: the cube is not turned inside out.
    assert np.linalg.det(m[:3, :3]) > 0


def test_a_downward_box_and_a_roll_stay_orthogonal():
    m = _matrix(box_matrix((0, 0, 0), (0.0, -1.0, 0.0), 50.0, 30.0, roll=1.1))
    axes = m[:3, :3] / np.linalg.norm(m[:3, :3], axis=0)
    assert np.allclose(axes.T @ axes, np.eye(3), atol=1e-6)
    assert np.allclose(axes[:, 2], (0.0, -1.0, 0.0))


def test_decals_fade_over_the_last_part_of_their_life():
    table = DecalTable()
    assert table.frame(0.0, 90.0, 20.0) is None
    table.add((0, 0, 0), (0, 0, 1), 40.0, 20.0, 3, now=100.0, kind=FLOOR, flip=True)
    models, params, _inverses = table.frame(101.0, 90.0, 20.0)
    assert models.shape == (1, 16) and models.dtype == np.float32
    assert params.tolist() == [[3.0, 1.0, float(FLOOR), 1.0]]
    assert table.frame(180.0, 90.0, 20.0)[1][0, 1] == pytest.approx(0.5)
    assert table.frame(190.5, 90.0, 20.0) is None


def test_the_ring_overwrites_the_oldest_and_a_frame_is_a_copy():
    table = DecalTable(capacity=4)
    for i in range(6):
        table.add((float(i), 0, 0), (0, 0, 1), 10.0, 10.0, i, now=float(i))
    models, params, _inverses = table.frame(6.0, 90.0, 20.0)
    assert params[:, 0].tolist() == [2.0, 3.0, 4.0, 5.0]      # oldest first
    assert params[:, 2].tolist() == [float(WALL)] * 4
    models[:] = 0.0
    assert table.model.any()
    table.clear()
    assert table.frame(6.0, 90.0, 20.0) is None and table.count == 6
