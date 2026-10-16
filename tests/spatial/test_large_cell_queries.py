"""Finite distant cells must not alias other cells in batched collision queries."""

import numpy as np
import pytest

from engine.physics import SpatialGrid, _CellRows, _CELL_OFFSET, _CELL_STRIDE
from tests.helpers.worlds import box_brush


@pytest.mark.parametrize('sign', [-1, 1])
@pytest.mark.parametrize('reverse', [False, True])
def test_batch_queries_match_scalar_for_cells_with_colliding_packed_keys(sign, reverse):
    coordinates = [(0, sign * _CELL_STRIDE), (sign, 0)]
    centers = [(cx * 512.0 + 256, cz * 512.0 + 256) for cx, cz in coordinates]
    brushes = [box_brush(str(i), (x, i * 32., z), (64., 32., 64.))
               for i, (x, z) in enumerate(centers)]
    grid = SpatialGrid()
    grid.populate(list(reversed(brushes)) if reverse else brushes)
    x, z = np.array(centers).T
    expected = [grid.raycast_down(px, pz, 1000) for px, pz in centers]
    assert all(height is not None for height in expected)
    np.testing.assert_array_equal(grid.raycast_down_batch(x, z, 1000), expected)
    expected_walls = [grid.overlaps_wall(px, 0., pz, 16.) for px, pz in centers]
    assert all(expected_walls)
    np.testing.assert_array_equal(grid.overlaps_wall_batch(x, np.zeros(2), z, 16.),
                                  expected_walls)


@pytest.mark.parametrize('query', [(-1, _CELL_STRIDE), (1, -_CELL_STRIDE),
                                 (1 << 45, 0), (0, -(1 << 80))])
def test_distant_query_does_not_match_a_normal_bucket(query):
    brush = box_brush('near')
    rows = _CellRows({(0, 0): [brush]})
    result = rows.pairs(np.array([0, query[0]], dtype=object),
                        np.array([0, query[1]], dtype=object))
    np.testing.assert_array_equal(result[0], [0])
    np.testing.assert_array_equal(result[1], [0])


@pytest.mark.parametrize('coord', [(-_CELL_OFFSET, -_CELL_OFFSET),
                                 (_CELL_OFFSET - 1, _CELL_OFFSET - 1),
                                 (_CELL_OFFSET, 0), (-_CELL_OFFSET - 1, 0),
                                 (1 << 80, -(1 << 80))])
def test_pair_rows_preserve_query_and_bucket_order_at_coordinate_limits(coord):
    first, second = box_brush('first'), box_brush('second')
    rows = _CellRows({coord: [second, first]})
    query, found = rows.pairs(np.array([coord[0], 0, coord[0]], dtype=object),
                              np.array([coord[1], 0, coord[1]], dtype=object))
    np.testing.assert_array_equal(query, [0, 0, 2, 2])
    assert [rows.brushes[i] for i in found] == [second, first, second, first]


def test_nonfinite_batch_coordinates_remain_misses():
    grid = SpatialGrid()
    grid.populate([box_brush('near', (256, 0, 256))])
    x = np.array([np.nan, np.inf, -np.inf, 256.])
    z = np.full(4, 256.)
    np.testing.assert_array_equal(grid.raycast_down_batch(x, z, 1000),
                                  [np.nan, np.nan, np.nan, 32.])
    np.testing.assert_array_equal(grid.overlaps_wall_batch(x, np.zeros(4), z, 16),
                                  [False, False, False, True])
