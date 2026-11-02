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
    CellGroupIndex,
    group_rows_by_cell,
    select_probe_groups,
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
    assert book.culled(gone, OCCLUDE_AFTER)

    book.retire([keep])
    assert len(book) == 1
    assert not book.culled(gone, OCCLUDE_AFTER + 1)
    assert book.culled(keep, OCCLUDE_AFTER + 1)


def test_book_defaults_match_module_constants():
    book = OcclusionProbeBook()
    assert book.occlude_after == OCCLUDE_AFTER
    assert book.max_result_age == MAX_RESULT_AGE


def test_changed_cell_forgets_old_hidden_answers():
    book = OcclusionProbeBook()
    coord = (4, -7)
    book.record(coord, 0, 1)
    book.record(coord, 0, 2)
    assert book.culled(coord, 3)
    book.forget(coord)
    assert not book.culled(coord, 3)
    book.record(coord, 0, 4)
    assert not book.culled(coord, 5)


def test_probe_selection_excludes_near_small_groups_and_never_exceeds_budget():
    counts = (30, 3, 40, 21)
    group_of_row = np.repeat(np.arange(4), counts)
    # Group 0 is near-exempt, group 1 too small to repay a query.
    near_sq = np.array([0.0, 300.0, 500.0, 200.0])
    result = select_probe_groups(
        group_of_row, near_sq, 2, near_exempt_sq=100.0,
        min_group_rows=16)
    assert result.tolist() == [2, 3]
    assert select_probe_groups(
        group_of_row, near_sq, 1, near_exempt_sq=100.0,
        min_group_rows=16).tolist() == [2]
    assert not len(select_probe_groups(
        group_of_row, near_sq, 0, near_exempt_sq=100.0))


def test_probe_selection_uses_3d_aabb_distance_not_xy_only():
    # The caller's distance uses the eye-to-BOX clamped 3-D distance,
    # so X/Z motion changes priority as intended.
    counts = (20, 20)
    mapping = np.repeat(np.arange(2), counts)
    result = select_probe_groups(
        mapping, np.array([400.0, 100.0]), 1, near_exempt_sq=0.0)
    assert result.tolist() == [1]


def test_probe_selection_no_groups_or_no_useful_groups():
    empty = select_probe_groups(
        np.empty(0, np.intp), np.empty(0), 8, near_exempt_sq=0.0)
    assert empty.shape == (0,)
    only_near = select_probe_groups(
        np.zeros(20, np.intp), np.array([1.0]), 8,
        near_exempt_sq=100.0)
    assert not len(only_near)



# -- Change-driven cache (the per-frame path must NOT sort brushes) --------


class _FakeRenderTable:
    def __init__(self, bounds):
        self.bounds = np.asarray(bounds, dtype=np.float64).copy()
        self.count = len(self.bounds)
        self.ids = [f"cell-brush-{n}" for n in range(self.count)]
        self.rot = np.zeros((self.count, 4), dtype=np.float32)
        self.hidden = np.zeros(self.count, dtype=bool)
        self.geometry_id = np.full(self.count, -1, dtype=np.int32)
        self.class_bits = np.zeros(self.count, dtype=np.uint16)
        self.geo_epoch = np.zeros(self.count, dtype=np.int64)


def _visible_cells(index, slots):
    got = index.visible(slots)
    assert got is not None
    eligible, boxes, row_groups, cells, counts, signatures = got
    return eligible, boxes, row_groups, cells, counts, signatures


def test_cell_index_matches_direct_grouping_without_per_frame_rebuild(scene_bounds):
    table = _FakeRenderTable(scene_bounds)
    index = CellGroupIndex(table)
    assert index.rebuilds == 1
    slots = np.arange(table.count, dtype=np.intp)
    mask, boxes, row_groups, cells, counts, signatures = _visible_cells(index, slots)
    direct_boxes, direct_groups, direct_cells = group_rows_by_cell(scene_bounds)
    np.testing.assert_array_equal(cells, direct_cells)
    np.testing.assert_array_equal(row_groups, direct_groups)
    np.testing.assert_allclose(boxes, direct_boxes)
    np.testing.assert_array_equal(counts, np.bincount(
        direct_groups, minlength=len(direct_cells)))
    assert mask.all()
    for _ in range(100):
        _visible_cells(index, slots)
    assert index.rebuilds == 1
    assert index.changed_groups == 0


def test_cell_index_updates_only_touched_cells_for_move_and_hidden():
    table = _FakeRenderTable([
        [100, 0, 100, 10, 10, 10],
        [150, 0, 100, 10, 10, 10],
        [800, 0, 100, 10, 10, 10],
    ])
    index = CellGroupIndex(table)
    old = _visible_cells(index, [0, 1, 2])
    table.bounds[1, 0] = 900
    index.update(table, [1])
    assert index.rebuilds == 1
    assert index.changed_groups == 2
    moved = _visible_cells(index, [0, 1, 2])
    assert tuple(moved[3][moved[2][1]]) == (1, 0)
    assert tuple(moved[3][moved[2][0]]) == (0, 0)
    assert moved[5][0] != old[5][0]

    table.hidden[2] = True
    index.update(table, [2])
    after_hide = _visible_cells(index, [0, 1, 2])
    assert after_hide[0].tolist() == [True, True, False]
    assert index.rebuilds == 1


def test_cell_index_fail_open_for_rotated_and_convex_brushes():
    table = _FakeRenderTable([
        [100, 0, 100, 10, 10, 10],
        [150, 0, 150, 10, 10, 10],
        [170, 0, 170, 10, 10, 10],
    ])
    table.rot[1, 3] = 30
    table.geometry_id[2] = 4
    index = CellGroupIndex(table)
    mask, boxes, group, cells, counts, signatures = _visible_cells(index, [0, 1, 2])
    assert mask.tolist() == [True, False, False]
    assert counts.tolist() == [1]
    table.rot[1, 3] = 0
    table.geometry_id[2] = -1
    index.update(table, [1, 2])
    assert _visible_cells(index, [0, 1, 2])[0].all()


def test_cached_group_signature_detects_content_change_without_box_change():
    table = _FakeRenderTable([
        [100, 0, 100, 10, 10, 10],
        [100, 0, 100, 10, 10, 10],
    ])
    index = CellGroupIndex(table)
    first = _visible_cells(index, [0, 1])[5][0]
    table.class_bits[0] = 8
    index.update(table, [0])
    second = _visible_cells(index, [0, 1])[5][0]
    assert first != second
    assert index.rebuilds == 1


def test_cell_index_new_groups_and_rebuild_on_large_dirty_batches():
    table = _FakeRenderTable(np.tile(
        np.array([100., 0., 100., 10., 10., 10.]), (1024, 1)))
    index = CellGroupIndex(table)
    table.bounds[0, 0] = 900
    index.update(table, [0])
    assert len(index.cells) == 2
    assert index.rebuilds == 1
    table.bounds[200:700, 0] = 1500
    index.update(table, range(200, 700))
    assert index.rebuilds == 2
    assert set(map(tuple, _visible_cells(
        index, np.arange(1024))[3])) == {(0, 0), (1, 0), (2, 0)}
