"""Headless tests for engine.cell_cull -- grouping and probe bookkeeping.

No GL context is involved: :func:`group_rows_by_cell` is pure NumPy and
:class:`OcclusionProbeBook` holds plain integers, so the rules the occlusion
feature depends on -- a group box contains its members, a group is the cell of
its members' centres, and the book only ever culls on fresh, consecutive,
completed answers -- are pinned here the way ``test_render_cull.py`` pins the
frustum test.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.cell_cull import (  # noqa: E402
    MAX_RESULT_AGE,
    OCCLUDE_AFTER,
    OcclusionProbeBook,
    group_rows_by_cell,
)
from engine.spatial import CELL_SIZE, cell_of_point  # noqa: E402


def _bounds(rng, count):
    """Random (N, 6) centre/half rows scattered over a few dozen cells."""
    centers = rng.uniform(-4096.0, 4096.0, (count, 3))
    halves = rng.uniform(8.0, 180.0, (count, 3))
    return np.concatenate((centers, halves), axis=1)


def _minmax(rows):
    """The (mins, maxs) corner form of (N, 6) centre/half rows."""
    return rows[:, :3] - rows[:, 3:], rows[:, :3] + rows[:, 3:]


@pytest.fixture
def scene_bounds():
    return _bounds(np.random.default_rng(20260927), 512)


# -- group_rows_by_cell ----------------------------------------------------


def test_group_box_contains_every_member_box(scene_bounds):
    """The union box must contain every row filed under it.

    This is the invariant the occlusion probes lean on: a box that fails a
    depth test means every brush inside it fails it too, which is only true
    if the box really contains them.
    """
    group_bounds, group_of_row, _ = group_rows_by_cell(scene_bounds)
    mins, maxs = _minmax(scene_bounds)
    gmins, gmaxs = _minmax(group_bounds)
    per_row_min = gmins[group_of_row]
    per_row_max = gmaxs[group_of_row]
    assert np.all(per_row_min <= mins) and np.all(maxs <= per_row_max)


def test_every_row_lands_in_exactly_one_group(scene_bounds):
    count = len(scene_bounds)
    group_bounds, group_of_row, group_cells = group_rows_by_cell(scene_bounds)
    assert len(group_bounds) == len(group_cells)
    assert len(group_of_row) == count
    # Every row's group's cell is the cell of the row's centre -- the exact
    # convention engine.spatial owns, checked against cell_of_point itself.
    for i in range(count):
        cx, cz = group_cells[group_of_row[i]]
        assert (cx, cz) == cell_of_point(scene_bounds[i, 0], scene_bounds[i, 2])


def test_groups_are_by_centre_not_footprint():
    """A brush straddling a cell edge belongs to its centre's cell only.

    Unlike the streaming cell index (a brush files under every cell it
    touches), grouping needs each row in exactly one group, and the group
    box still contains the straddler.
    """
    edge = 2.0 * CELL_SIZE
    bounds = np.array([
        [edge + 1.0, 0.0, 0.0, CELL_SIZE, 16.0, 16.0],  # centre right of edge
        [edge - 400.0, 0.0, 0.0, 100.0, 16.0, 16.0],    # centre left of edge
    ])
    group_bounds, group_of_row, group_cells = group_rows_by_cell(bounds)
    assert len(group_bounds) == 2
    assert group_of_row[0] != group_of_row[1]
    assert group_cells[group_of_row[0]][0] == 2
    assert group_cells[group_of_row[1]][0] == 1
    mins, maxs = _minmax(bounds)
    gmins, gmaxs = _minmax(group_bounds)
    assert np.all(gmins[group_of_row] <= mins)
    assert np.all(maxs <= gmaxs[group_of_row])


def test_negative_and_far_coordinates():
    """Floor, not truncation: -0.5 is in cell -1 at any world offset."""
    centers = np.array([
        [-0.5, -0.5, -0.5],
        [-(2 ** 30), 0.0, 0.0],          # far away, still a valid int64 cell
        [1.5 * CELL_SIZE, 0.0, -2.5 * CELL_SIZE],
    ])
    bounds = np.concatenate((centers, np.full((3, 3), 8.0)), axis=1)
    _, group_of_row, group_cells = group_rows_by_cell(bounds)
    for i in range(3):
        cx, cz = group_cells[group_of_row[i]]
        assert (cx, cz) == cell_of_point(centers[i][0], centers[i][2])
    assert len(group_cells) == 3  # all in different cells


def test_empty_and_single_row_inputs():
    empty_bounds, group_of_row, group_cells = group_rows_by_cell(
        np.zeros((0, 6)))
    assert empty_bounds.shape == (0, 6)
    assert group_of_row.shape == (0,)
    assert group_cells.shape == (0, 2)

    single = np.array([[100.0, 0.0, 100.0, 32.0, 32.0, 32.0]])
    group_bounds, group_of_row, group_cells = group_rows_by_cell(single)
    assert len(group_bounds) == 1
    assert group_of_row[0] == 0
    assert tuple(group_cells[0]) == cell_of_point(single[0, 0], single[0, 2])
    assert np.allclose(group_bounds[0], single[0])


def test_identical_centres_share_one_group():
    bounds = np.tile([256.0, 10.0, 256.0, 32.0, 8.0, 32.0], (7, 1))
    group_bounds, group_of_row, group_cells = group_rows_by_cell(bounds)
    assert len(group_bounds) == 1
    assert np.all(group_of_row == 0)
    assert len(group_cells) == 1


# -- OcclusionProbeBook -----------------------------------------------------


def test_book_never_culls_on_pending_or_single_answers():
    """Two consecutive zeros are required; one is not a hiding."""
    book = OcclusionProbeBook()
    coord = (3, -4)
    assert not book.culled(coord, 0)
    book.record(coord, 0, 1)
    assert not book.culled(coord, 2)
    book.record(coord, 0, 3)
    assert book.culled(coord, 4)


def test_book_visible_result_clears_the_streak():
    """Being seen once outranks any number of earlier hidings."""
    book = OcclusionProbeBook()
    coord = (0, 0)
    for frame in range(OCCLUDE_AFTER):
        book.record(coord, 0, frame)
    assert book.culled(coord, OCCLUDE_AFTER)
    book.record(coord, 0, 10)
    book.record(coord, 120, 11)  # a pixel survived
    assert not book.culled(coord, 12)
    # And the streak starts over rather than resuming.
    book.record(coord, 0, 13)
    assert not book.culled(coord, 14)


def test_book_fails_open_on_stale_results():
    """An answer for a view the camera has left stops culling."""
    book = OcclusionProbeBook()
    coord = (1, 1)
    for frame in range(OCCLUDE_AFTER):
        book.record(coord, 0, frame)
    last = OCCLUDE_AFTER - 1
    assert book.culled(coord, last + MAX_RESULT_AGE)
    assert not book.culled(coord, last + MAX_RESULT_AGE + 1)


def test_book_retires_cells_that_left_the_view():
    book = OcclusionProbeBook()
    keep, gone = (2, 2), (5, 5)
    for frame in range(OCCLUDE_AFTER):
        book.record(keep, 0, frame)
        book.record(gone, 0, frame)
    assert book.culled(gone, 10)

    book.retire([keep])
    assert len(book) == 1
    assert not book.culled(gone, 11)
    assert book.culled(keep, 11)


def test_book_defaults_match_module_constants():
    book = OcclusionProbeBook()
    assert book.occlude_after == OCCLUDE_AFTER
    assert book.max_result_age == MAX_RESULT_AGE
