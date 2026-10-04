"""Line of sight: the dense path must answer exactly what the scalar one does.

``SpatialGrid.has_line_of_sight`` has two ways through the brushes a ray's
cells hold. The per-brush path deduplicates them through a set of ``id()``,
reads each one's AABB from its dict and runs the slab test in Python; the dense
path tests the static ones as rows of bounds the grid copied when it was
populated, all at once, and walks only the movers and doors.

They are one answer or the change moved a wall. The tests here drive both over
the same grid and compare, with the cases most likely to separate them:

* **grazing rays**, because the slab test is a comparison, and bounds derived
  any other way than through ``brush_aabb_bounds`` (float32, via ``glm.vec3``)
  differ by up to 3e-04 -- enough to decide one;
* **mesh-collision brushes**, because the grid files those by their mesh bounds
  while the narrow phase tests pos/size, so the cell a brush is *in* and the
  box that is *tested* are deliberately different things;
* **movers**, which move while the grid stands still.

The rows are the grid's own. Line of sight runs on the monster AI thread, and
it used to read the renderer's :class:`~engine.render_table.RenderTable`
instead -- a table the logic thread refreshes and reconciles every tick, and
which the double-buffered publication recycles one frame after the AI saw it.
"""

import contextlib
import math

import numpy as np
import pytest

pytest.importorskip("glm")

import glm                                                   # noqa: E402

from engine.physics import SpatialGrid                       # noqa: E402
from tests.helpers.worlds import box_brush                   # noqa: E402


def _world(brushes):
    grid = SpatialGrid()
    grid.populate(brushes)
    return grid


@contextlib.contextmanager
def _forcing_dense(grid):
    """Send every ray down the dense narrow phase, whatever its size.

    Production picks between the two narrow phases on candidate count, and a
    test scene is far below the threshold -- so without this every "dense"
    assertion in this file would quietly be running the scalar walk and
    comparing it with itself. Zero means "always dense"; the gate is a speed
    knob and both paths answer the same, which is what these tests establish.
    """
    grid.LOS_DENSE_MIN_CANDIDATES = 0
    try:
        yield
    finally:
        del grid.LOS_DENSE_MIN_CANDIDATES


def _both(grid, start, end):
    """(scalar answer, dense answer) for one ray, one narrow phase each."""
    scalar = grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end))
    with _forcing_dense(grid):
        dense = grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end))
    return scalar, dense


def _agree(grid, start, end, what):
    scalar, dense = _both(grid, start, end)
    assert scalar == dense, (
        "%s: the per-brush path said %s and the dense path said %s for the ray "
        "%s -> %s" % (what, scalar, dense, start, end))
    return scalar


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

def test_every_static_brush_has_one_row_holding_its_tested_bounds():
    from engine.constants import brush_aabb_bounds

    brushes = [box_brush("w%d" % i, (i * 200.0, 0.0, 0.0)) for i in range(5)]
    brushes.append(box_brush("long", (0.0, 0.0, 0.0), (32.0, 64.0, 3000.0)))
    grid = _world(brushes)
    rows = grid._los_rows

    assert len(rows.lo) == len(brushes), (
        "a brush filed under several cells must still be one row")
    for coord, bucket in grid.cells.items():
        for brush, row in zip(bucket, rows.static_by_cell[coord]):
            bounds = brush_aabb_bounds(brush)
            assert tuple(rows.lo[row]) == bounds[:3]
            assert tuple(rows.hi[row]) == bounds[3:]


def test_movers_and_doors_are_walked_not_copied():
    lift = box_brush("lift", (0.0, 0.0, 0.0), is_mover=True)
    door = box_brush("door", (100.0, 0.0, 0.0), is_door=True)
    grid = _world([lift, door, box_brush("wall", (300.0, 0.0, 0.0))])
    rows = grid._los_rows

    assert len(rows.lo) == 1
    assert [b["name"] for b in rows.moving_by_cell[(0, 0)]] == ["lift", "door"]


def test_populate_replaces_the_rows_rather_than_editing_them():
    """A ray in flight on the AI thread keeps the rows it started with."""
    brushes = [box_brush("wall", (0.0, 0.0, 0.0))]
    grid = _world(brushes)
    first = grid._los_rows
    lo = first.lo.copy()
    cells = dict(first.brushes_by_cell)

    grid.populate(brushes + [box_brush("more", (900.0, 0.0, 0.0))])

    assert grid._los_rows is not first
    assert len(grid._los_rows.lo) == 2
    np.testing.assert_array_equal(first.lo, lo)
    assert first.brushes_by_cell == cells


def test_clearing_the_grid_clears_line_of_sight():
    grid = _world([box_brush("wall", (0.0, 0.0, 0.0), (32.0, 256.0, 512.0))])
    assert grid.has_line_of_sight(glm.vec3(-300, 0, 0), glm.vec3(300, 0, 0)) is False
    grid.clear()
    assert grid.has_line_of_sight(glm.vec3(-300, 0, 0), glm.vec3(300, 0, 0)) is True


def test_the_monster_ai_does_not_read_the_render_table():
    """The production MonsterAI operates from the real LogicThread-owned grid."""
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    logic = LogicThread(ThreadedGameState(), EditorState())
    logic.monster_ai._grid = _world(
        [box_brush("wall", (0.0, 0.0, 0.0), (32.0, 256.0, 512.0))]
    )

    try:
        ai = logic.monster_ai
        assert ai._has_line_of_sight(glm.vec3(-300, 0, 0), glm.vec3(300, 0, 0)) is False
        assert ai._has_line_of_sight(glm.vec3(-300, 0, 900), glm.vec3(300, 0, 900)) is True
    finally:
        logic.stop()


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

def test_a_wall_blocks_both_paths():
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (32.0, 256.0, 512.0))]
    grid = _world(brushes)
    assert _agree(grid, (-300, 0, 0), (300, 0, 0), "through a wall") is False


def test_a_clear_line_passes_both_paths():
    brushes = [box_brush("wall", (0.0, 0.0, 900.0), (32.0, 256.0, 128.0))]
    grid = _world(brushes)
    assert _agree(grid, (-300, 0, 0), (300, 0, 0), "clear line") is True


def test_a_ray_that_stops_short_of_the_wall_reaches_neither():
    """`limit = ray_len - 0.1`: a hit past the end is not a hit."""
    brushes = [box_brush("wall", (500.0, 0.0, 0.0), (32.0, 256.0, 256.0))]
    grid = _world(brushes)
    assert _agree(grid, (0, 0, 0), (100, 0, 0), "ray stops short") is True
    assert _agree(grid, (0, 0, 0), (600, 0, 0), "ray reaches") is False


@pytest.mark.parametrize("offset", [
    -0.5, -0.05, -0.005, -5e-4, -5e-5, 0.0, 5e-5, 5e-4, 0.005, 0.05, 0.5,
])
def test_a_ray_grazing_a_face_agrees_to_the_last_ulp(offset):
    """The case the float32 rounding decides.

    The scalar path's bounds come through glm.vec3 and are float32; deriving
    them in float64 from the same columns differs by up to 3.6e-04, which at
    these offsets is the difference between hitting and missing.
    """
    size = 127.3
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (size, size, size))]
    grid = _world(brushes)
    edge = size * 0.5
    _agree(grid, (-400.0, edge + offset, 0.0), (400.0, edge + offset, 0.0),
           "grazing the top face by %g" % offset)
    _agree(grid, (-400.0, 0.0, edge + offset), (400.0, 0.0, edge + offset),
           "grazing the side face by %g" % offset)


def test_a_ray_between_the_float32_and_float64_faces_agrees():
    """The float32 claim, made decisive rather than left to luck.

    ``brush_aabb_bounds`` rounds a brush's half-extent through float32, so for
    a large brush its top face sits a fraction above where a float64
    derivation from the same columns would put it. A ray threaded *between*
    those two heights is inside the box the scalar path tests and outside the
    box a float64 dense path would test -- so it separates the two
    implementations by construction, not by chance.
    """
    from engine.constants import brush_aabb_bounds

    size = 1500.3
    wall = box_brush("big", (0.0, 40.0, 0.0), (size, size, size))
    grid = _world([wall])

    glm_hi_y = brush_aabb_bounds(wall)[4]
    float64_hi_y = wall["pos"][1] + wall["size"][1] * 0.5
    assert glm_hi_y > float64_hi_y, (
        "this scene no longer separates the two roundings (%r vs %r)"
        % (glm_hi_y, float64_hi_y))

    between = (glm_hi_y + float64_hi_y) * 0.5
    assert float64_hi_y < between < glm_hi_y

    scalar, dense = _both(grid, (-4000.0, between, 0.0),
                          (4000.0, between, 0.0))
    assert scalar is False, "the ray should be inside the float32 box"
    assert dense == scalar, (
        "the dense path said %s where the per-brush path said %s -- it is not "
        "reproducing the float32 rounding of brush_aabb_bounds" % (dense, scalar))


def test_an_axis_parallel_ray_agrees():
    """The degenerate branch: parallel to a slab, inside it or rejected."""
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (64.0, 64.0, 64.0))]
    grid = _world(brushes)
    for start, end, what in (
            ((-400, 0, 0), (400, 0, 0), "along +x through the box"),
            ((-400, 200, 0), (400, 200, 0), "along +x above the box"),
            ((0, -400, 0), (0, 400, 0), "along +y through the box"),
            ((0, 0, -400), (0, 0, 400), "along +z through the box"),
            ((-400, 32.0, 0), (400, 32.0, 0), "along +x exactly on the top face")):
        _agree(grid, start, end, what)


def test_a_mesh_collision_brush_is_filed_by_mesh_but_tested_by_size():
    """The grid's cell membership and the narrow phase read different boxes.

    A mesh brush is inserted into the cells its *mesh* bounds cover, while both
    line-of-sight paths test its pos/size box. The dense path takes its cells
    from the same buckets and its box from the same columns, so it inherits
    both halves -- this pins that it does.
    """
    wall = box_brush("mesh_wall", (0.0, 0.0, 0.0), (64.0, 256.0, 64.0))
    wall["_collision_mode"] = "mesh"
    wall["_mesh_bounds"] = ([-700.0, -128.0, -700.0], [700.0, 128.0, 700.0])
    brushes = [wall]
    grid = _world(brushes)

    occupied = len(grid.cells)
    assert occupied > 1, (
        "the mesh bounds should span several cells; got %d" % occupied)
    assert _agree(grid, (-300, 0, 0), (300, 0, 0), "mesh brush, through") is False
    assert _agree(grid, (-300, 0, 600), (300, 0, 600),
                  "mesh brush, past its pos/size box but inside its mesh bounds") is True


def test_a_moved_mover_is_seen_live_by_both_paths():
    lift = box_brush("lift", (0.0, 0.0, 0.0), (64.0, 256.0, 256.0), is_mover=True)
    brushes = [lift]
    grid = _world(brushes)
    assert _agree(grid, (-300, 0, 0), (300, 0, 0), "mover in the way") is False

    lift["pos"] = [0.0, 900.0, 0.0]
    assert _agree(grid, (-300, 0, 0), (300, 0, 0), "mover lifted away") is True


def test_the_two_paths_agree_over_a_random_sweep():
    """Breadth, against a scene with enough brushes to fill several cells."""
    rng = np.random.default_rng(11)
    brushes = []
    for i in range(120):
        pos = rng.uniform(-1200, 1200, 3)
        size = rng.uniform(16, 320, 3)
        brushes.append(box_brush("b%d" % i, tuple(pos), tuple(size)))
    grid = _world(brushes)

    disagreements = []
    blocked = 0
    for _ in range(400):
        start = tuple(rng.uniform(-1500, 1500, 3))
        end = tuple(rng.uniform(-1500, 1500, 3))
        scalar, dense = _both(grid, start, end)
        if scalar != dense:
            disagreements.append((start, end, scalar, dense))
        blocked += not scalar

    assert not disagreements, (
        "%d of 400 rays disagreed; first %s" % (len(disagreements),
                                                disagreements[0]))
    assert 20 < blocked < 380, (
        "only %d of 400 rays were blocked -- the sweep is not exercising both "
        "answers" % blocked)


# ---------------------------------------------------------------------------
# The cell traversal
#
# The grid used to sample the ray every `cell_size` and test each sample's
# eight neighbours; it now steps the cell boundaries the ray actually crosses.
# The fan is gone from the engine, so the reference lives here: `_fan_cells` is
# the traversal it did, and `_fan_answer` runs the *real* line of sight with
# only that swapped in. Nothing else differs between reference and subject, so
# a disagreement is the traversal's and nothing else's.
# ---------------------------------------------------------------------------

def _fan_cells(grid, start, ray_dir, ray_len):
    """The pre-DDA traversal: sampled points, each with its eight neighbours."""
    cell_size = grid.cell_size
    steps = max(1, int(ray_len / cell_size) + 2)
    out = []
    for i in range(steps + 1):
        t = min(i / float(steps), 1.0) * ray_len
        pt = start + ray_dir * t
        cx = int(math.floor(pt.x / cell_size))
        cz = int(math.floor(pt.z / cell_size))
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                out.append((cx + dx, cz + dz))
    return out


def _sampled_cells(grid, start, ray_dir, ray_len):
    """The same sampling with the fan removed -- i.e. the bug the fan hid."""
    cell_size = grid.cell_size
    steps = max(1, int(ray_len / cell_size) + 2)
    out = []
    for i in range(steps + 1):
        t = min(i / float(steps), 1.0) * ray_len
        pt = start + ray_dir * t
        out.append((int(math.floor(pt.x / cell_size)),
                    int(math.floor(pt.z / cell_size))))
    return out


@contextlib.contextmanager
def _traversal(fn):
    """Run line of sight with `fn` deciding which cells the ray visits."""
    real = SpatialGrid._cells_along_ray
    SpatialGrid._cells_along_ray = fn
    try:
        yield
    finally:
        SpatialGrid._cells_along_ray = real


def _fan_answer(grid, start, end):
    with _traversal(_fan_cells):
        return grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end))


def _sampled_answer(grid, start, end):
    with _traversal(_sampled_cells):
        return grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end))


def _matches_the_fan(grid, start, end, what):
    """Both of today's paths must answer what the fan answered."""
    reference = _fan_answer(grid, start, end)
    scalar, dense = _both(grid, start, end)
    assert scalar == reference, (
        "%s: the fan said %s and the per-brush path now says %s for %s -> %s"
        % (what, reference, scalar, start, end))
    assert dense == reference, (
        "%s: the fan said %s and the dense path now says %s for %s -> %s"
        % (what, reference, dense, start, end))
    return reference


def _walk(grid, start, end):
    """The cells the traversal reports for a segment, in order."""
    direction = glm.vec3(*end) - glm.vec3(*start)
    length = glm.length(direction)
    return grid._cells_along_ray(glm.vec3(*start), direction / length, length)


# --- the case the fan existed for ------------------------------------------

#: A ray that leaves cell (0,0) across x=512 and re-enters across z=512, so it
#: clips the corner of cell (1,0) between two sample points. Derived by
#: stepping the old sampler: with this length the samples land in (0,0) and
#: then (1,1), and (1,0) -- the only cell this brush is filed under -- is never
#: looked at without the fan.
_GAP_START = (10.0, 0.0, 10.0)
_GAP_END = (3726.0, 0.0, 3355.0)
_GAP_BRUSH = ("clip", (540.0, 0.0, 490.0), (40.0, 256.0, 36.0))


def test_the_sampling_gap_the_fan_was_added_for_is_real():
    """Guard the guard: without the fan, plain sampling misses this brush.

    If this ever starts passing as visible==False, the case has stopped being
    a gap and the test below it is no longer proving anything.
    """
    grid = _world([box_brush(*_GAP_BRUSH)])

    sampled = _sampled_cells(grid, glm.vec3(*_GAP_START),
                             glm.normalize(glm.vec3(*_GAP_END)
                                           - glm.vec3(*_GAP_START)),
                             glm.length(glm.vec3(*_GAP_END)
                                        - glm.vec3(*_GAP_START)))
    assert (1, 0) not in sampled, (
        "the sampled points now include (1,0), so this ray no longer skips a "
        "cell and cannot demonstrate the gap")
    assert _sampled_answer(grid, _GAP_START, _GAP_END) is True, (
        "sampling alone was supposed to miss the occluder")
    assert _fan_answer(grid, _GAP_START, _GAP_END) is False, (
        "the fan was supposed to catch it")


def test_the_traversal_closes_the_sampling_gap():
    """The reason the fan can go: the skipped cell is now walked, not guessed."""
    grid = _world([box_brush(*_GAP_BRUSH)])

    assert (1, 0) in _walk(grid, _GAP_START, _GAP_END), (
        "the traversal skipped the cell the ray crosses")
    assert _matches_the_fan(grid, _GAP_START, _GAP_END,
                            "the diagonal straddle") is False


# --- the traversal's own shape ---------------------------------------------

def test_a_ray_inside_one_cell_visits_only_that_cell():
    grid = SpatialGrid()
    assert _walk(grid, (100, 0, 100), (300, 0, 200)) == [(0, 0)]


def test_a_vertical_ray_visits_only_the_column_it_is_in():
    """No XZ movement at all -- the degenerate case of the stepping loop."""
    grid = SpatialGrid()
    assert _walk(grid, (100, -500, 100), (100, 500, 100)) == [(0, 0)]


def test_the_walk_is_contiguous_and_ends_where_the_ray_does():
    """Every step moves one cell on one axis, and the last is the end cell."""
    rng = np.random.default_rng(5)
    grid = SpatialGrid()
    for _ in range(300):
        start = tuple(rng.uniform(-3000, 3000, 3))
        end = tuple(rng.uniform(-3000, 3000, 3))
        if glm.length(glm.vec3(*end) - glm.vec3(*start)) < 0.001:
            continue
        cells = _walk(grid, start, end)
        assert cells[0] == (int(math.floor(start[0] / grid.cell_size)),
                            int(math.floor(start[2] / grid.cell_size)))
        assert cells[-1] == (int(math.floor(end[0] / grid.cell_size)),
                             int(math.floor(end[2] / grid.cell_size)))
        for a, b in zip(cells, cells[1:]):
            step = abs(a[0] - b[0]) + abs(a[1] - b[1])
            assert step == 1, (
                "the walk jumped from %s to %s -- a skipped cell is a missed "
                "occluder, which is the bug the fan was covering" % (a, b))


def test_the_walk_never_misses_a_cell_the_ray_is_inside():
    """Densely sample the segment; every sample's cell must be in the walk.

    This is the property the whole change rests on, checked directly rather
    than inferred: brushes are filed under every cell they overlap, so a ray
    that is ever inside a cell must look in it.
    """
    rng = np.random.default_rng(23)
    grid = SpatialGrid()
    for _ in range(200):
        start = glm.vec3(*rng.uniform(-2000, 2000, 3))
        end = glm.vec3(*rng.uniform(-2000, 2000, 3))
        length = glm.length(end - start)
        if length < 0.001:
            continue
        walked = set(_walk(grid, tuple(start), tuple(end)))
        for i in range(2001):
            pt = start + (end - start) * (i / 2000.0)
            cell = (int(math.floor(pt.x / grid.cell_size)),
                    int(math.floor(pt.z / grid.cell_size)))
            assert cell in walked, (
                "the ray %s -> %s passes through %s, which the walk %s does "
                "not visit" % (tuple(start), tuple(end), cell, sorted(walked)))


@pytest.mark.parametrize("name,start,end", [
    ("+x", (-1300.0, 0.0, 100.0), (1300.0, 0.0, 100.0)),
    ("-x", (1300.0, 0.0, 100.0), (-1300.0, 0.0, 100.0)),
    ("+z", (100.0, 0.0, -1300.0), (100.0, 0.0, 1300.0)),
    ("-z", (100.0, 0.0, 1300.0), (100.0, 0.0, -1300.0)),
    ("+x+z", (-1300.0, 0.0, -1300.0), (1300.0, 0.0, 1300.0)),
    ("-x-z", (1300.0, 0.0, 1300.0), (-1300.0, 0.0, -1300.0)),
    ("+x-z", (-1300.0, 0.0, 1300.0), (1300.0, 0.0, -1300.0)),
    ("-x+z", (1300.0, 0.0, -1300.0), (-1300.0, 0.0, 1300.0)),
])
def test_every_direction_walks_the_same_cells_either_way(name, start, end):
    """A segment's cells do not depend on which end you start from."""
    grid = SpatialGrid()
    assert set(_walk(grid, start, end)) == set(_walk(grid, end, start)), name


@pytest.mark.parametrize("name,start,end", [
    ("+x", (-1300.0, 0.0, 100.0), (1300.0, 0.0, 100.0)),
    ("-x", (1300.0, 0.0, 100.0), (-1300.0, 0.0, 100.0)),
    ("+z", (100.0, 0.0, -1300.0), (100.0, 0.0, 1300.0)),
    ("-z", (100.0, 0.0, 1300.0), (100.0, 0.0, -1300.0)),
    ("+x+z", (-1300.0, 0.0, -1300.0), (1300.0, 0.0, 1300.0)),
    ("-x-z", (1300.0, 0.0, 1300.0), (-1300.0, 0.0, -1300.0)),
    ("+x-z", (-1300.0, 0.0, 1300.0), (1300.0, 0.0, -1300.0)),
    ("-x+z", (1300.0, 0.0, -1300.0), (-1300.0, 0.0, 1300.0)),
])
def test_every_direction_answers_what_the_fan_answered(name, start, end):
    rng = np.random.default_rng(31)
    brushes = [box_brush("b%d" % i, tuple(rng.uniform(-1200, 1200, 3)),
                         tuple(rng.uniform(32, 400, 3))) for i in range(60)]
    grid = _world(brushes)
    _matches_the_fan(grid, start, end, name)


# --- boundaries, corners and grazes ----------------------------------------

def test_a_ray_that_starts_exactly_on_a_cell_boundary():
    """floor() puts the origin in the upper cell; the walk must start there."""
    grid = _world([box_brush("a", (700.0, 0.0, 100.0), (64.0, 256.0, 64.0)),
                          box_brush("b", (300.0, 0.0, 100.0), (64.0, 256.0, 64.0))])
    assert _walk(grid, (512.0, 0.0, 100.0), (900.0, 0.0, 100.0))[0] == (1, 0)
    _matches_the_fan(grid, (512.0, 0.0, 100.0), (900.0, 0.0, 100.0),
                     "starting on the boundary, forwards")
    _matches_the_fan(grid, (512.0, 0.0, 100.0), (100.0, 0.0, 100.0),
                     "starting on the boundary, backwards")


def test_a_ray_running_along_a_cell_boundary():
    """Degenerate: the ray never leaves the seam between two cell columns."""
    brushes = [box_brush("b%d" % i, (512.0, 0.0, i * 300.0 - 600.0),
                         (64.0, 256.0, 64.0)) for i in range(5)]
    grid = _world(brushes)
    _matches_the_fan(grid, (512.0, 0.0, -900.0), (512.0, 0.0, 900.0),
                     "along the x=512 seam")


def test_a_ray_through_an_exact_grid_corner():
    """All four cells are touched at one point; the tie must not pick two."""
    brushes = [box_brush("nw", (400.0, 0.0, 600.0), (64.0, 256.0, 64.0)),
               box_brush("ne", (600.0, 0.0, 600.0), (64.0, 256.0, 64.0)),
               box_brush("sw", (400.0, 0.0, 400.0), (64.0, 256.0, 64.0)),
               box_brush("se", (600.0, 0.0, 400.0), (64.0, 256.0, 64.0))]
    grid = _world(brushes)

    cells = set(_walk(grid, (12.0, 0.0, 12.0), (1012.0, 0.0, 1012.0)))
    for corner in ((0, 0), (1, 0), (0, 1), (1, 1)):
        assert corner in cells, (
            "the ray crosses the corner at (512,512) but the walk skipped %s; "
            "a brush filed only there would be invisible" % (corner,))
    _matches_the_fan(grid, (12.0, 0.0, 12.0), (1012.0, 0.0, 1012.0),
                     "straight through the corner")


def test_a_brush_spanning_several_cells_is_found_from_any_of_them():
    """A long wall is filed under every cell it crosses; each must block."""
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (32.0, 512.0, 3000.0))]
    grid = _world(brushes)
    for z in (-1400.0, -700.0, 0.0, 700.0, 1400.0):
        assert _matches_the_fan(grid, (-400.0, 0.0, z), (400.0, 0.0, z),
                                "through the wall at z=%g" % z) is False


def test_a_walk_through_empty_cells_is_clear():
    """Most cells hold nothing; `get` misses and the ray must still answer."""
    grid = _world([box_brush("far", (9000.0, 0.0, 9000.0))])
    assert len(set(_walk(grid, (-2000, 0, -2000), (2000, 0, 2000)))) > 4
    assert _matches_the_fan(grid, (-2000, 0, -2000), (2000, 0, 2000),
                            "across empty cells") is True


def test_grazing_rays_answer_what_the_fan_answered():
    """Rays aimed at a face, an edge and a corner of the same brush."""
    brushes = [box_brush("box", (600.0, 0.0, 600.0), (200.0, 200.0, 200.0))]
    grid = _world(brushes)
    for dx in (-100.0, -100.0000001, -99.9999999, 0.0, 100.0, 100.0000001):
        for dz in (-100.0, 0.0, 100.0, 100.0000001):
            start = (600.0 + dx, 0.0, -400.0)
            end = (600.0 + dx, 0.0, 1600.0)
            _matches_the_fan(grid, start, end,
                             "grazing at dx=%r dz=%r" % (dx, dz))
    for dy in (-100.0, -99.9999999, 0.0, 99.9999999, 100.0):
        _matches_the_fan(grid, (-400.0, dy, 600.0), (1600.0, dy, 600.0),
                         "grazing the top/bottom face at dy=%r" % dy)


def test_short_and_long_rays_answer_what_the_fan_answered():
    rng = np.random.default_rng(101)
    brushes = [box_brush("b%d" % i, tuple(rng.uniform(-4000, 4000, 3)),
                         tuple(rng.uniform(32, 600, 3))) for i in range(200)]
    grid = _world(brushes)
    for reach in (1.0, 20.0, 400.0, 512.0, 513.0, 2000.0, 9000.0):
        for _ in range(40):
            start = rng.uniform(-3000, 3000, 3)
            direction = rng.normal(size=3)
            direction /= np.linalg.norm(direction)
            _matches_the_fan(grid, tuple(start),
                             tuple(start + direction * reach),
                             "a ray of %g units" % reach)


def test_the_traversal_agrees_with_the_fan_over_a_random_sweep():
    """Breadth. Brushes small enough that a missed cell is a changed answer."""
    rng = np.random.default_rng(77)
    brushes = [box_brush("b%d" % i, tuple(rng.uniform(-2500, 2500, 3)),
                         tuple(rng.uniform(16, 200, 3))) for i in range(400)]
    grid = _world(brushes)

    differed = []
    blocked = 0
    for _ in range(600):
        start = tuple(rng.uniform(-2600, 2600, 3))
        end = tuple(rng.uniform(-2600, 2600, 3))
        reference = _fan_answer(grid, start, end)
        scalar, dense = _both(grid, start, end)
        if scalar != reference or dense != reference:
            differed.append((start, end, reference, scalar, dense))
        blocked += not reference

    assert not differed, (
        "%d of 600 rays changed answer; first (start, end, fan, scalar, dense) "
        "= %s" % (len(differed), differed[0]))
    assert 30 < blocked < 570, (
        "only %d of 600 rays were blocked -- the sweep is not exercising both "
        "answers" % blocked)


# ---------------------------------------------------------------------------
# Choosing a narrow phase
#
# The dense path costs a fixed ~58us of NumPy dispatch per ray and the scalar
# walk costs per brush, so which is faster is a question about the size of the
# candidate set. `LOS_DENSE_MIN_CANDIDATES` is where the answer changes. It is
# a speed knob and nothing else: the tests here pin that the answer does not
# depend on it, and that the count it compares against is the one the ray
# actually has.
# ---------------------------------------------------------------------------

def _path_taken(grid, start, end):
    """('dense'|'scalar', answer) -- which narrow phase decided this ray."""
    taken = []
    real = SpatialGrid._los_dense

    def spy(self, *args, **kwargs):
        out = real(self, *args, **kwargs)
        taken.append("scalar" if out is None else "dense")
        return out

    SpatialGrid._los_dense = spy
    try:
        answer = grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end))
    finally:
        SpatialGrid._los_dense = real
    return (taken[0] if taken else "scalar"), answer


def _candidate_count(grid, start, end):
    """The number the gate compares against, counted the way the gate does."""
    direction = glm.vec3(*end) - glm.vec3(*start)
    length = glm.length(direction)
    slots_by_cell = grid._los_rows.static_by_cell
    total = 0
    for coord in grid._cells_along_ray(glm.vec3(*start), direction / length,
                                       length):
        found = slots_by_cell.get(coord)
        if found is not None:
            total += len(found)
    return total


def _crowded_world(n=40):
    """Enough brushes in one cell to sit either side of a small threshold."""
    brushes = [box_brush("b%d" % i, (60.0 + i * 3.0, 0.0, 200.0 + i * 2.0),
                         (24.0, 200.0, 24.0)) for i in range(n)]
    return _world(brushes)


def test_the_gate_counts_the_candidates_the_ray_actually_has():
    grid = _crowded_world()
    start, end = (20.0, 0.0, 20.0), (400.0, 0.0, 400.0)
    counted = _candidate_count(grid, start, end)
    assert counted > 2, "the fixture stopped being crowded"

    grid.LOS_DENSE_MIN_CANDIDATES = counted
    assert _path_taken(grid, start, end)[0] == "dense", (
        "a ray with exactly the threshold count took the scalar path; the gate "
        "is off by one or is counting something else")
    grid.LOS_DENSE_MIN_CANDIDATES = counted + 1
    assert _path_taken(grid, start, end)[0] == "scalar", (
        "a ray one candidate short of the threshold still took the dense path")


def test_both_sides_of_the_threshold_give_the_same_answer():
    """The gate is a speed knob. Sweep it across the boundary and past it."""
    rng = np.random.default_rng(97)
    brushes = [box_brush("b%d" % i, tuple(rng.uniform(-900, 900, 3)),
                         tuple(rng.uniform(24, 260, 3))) for i in range(150)]
    grid = _world(brushes)

    rays = []
    for _ in range(120):
        rays.append((tuple(rng.uniform(-1100, 1100, 3)),
                     tuple(rng.uniform(-1100, 1100, 3))))

    blocked = 0
    for start, end in rays:
        if glm.length(glm.vec3(*end) - glm.vec3(*start)) < 0.001:
            continue
        counted = _candidate_count(grid, start, end)
        answers = {}
        for threshold in (0, max(0, counted - 1), counted, counted + 1,
                          counted + 1000):
            grid.LOS_DENSE_MIN_CANDIDATES = threshold
            path, answer = _path_taken(grid, start, end)
            answers[threshold] = (path, answer)
        distinct = {a for _, a in answers.values()}
        assert len(distinct) == 1, (
            "the ray %s -> %s with %d candidates answered %s depending on the "
            "threshold: %s" % (start, end, counted, distinct, answers))
        assert answers[0][0] == "dense" or counted == 0
        assert answers[counted + 1000][0] == "scalar"
        blocked += not distinct.pop()

    assert 10 < blocked < 110, (
        "only %d of 120 rays were blocked -- the sweep is not exercising both "
        "answers" % blocked)


def test_the_default_threshold_leaves_a_normal_scene_on_the_scalar_walk():
    """The regression this constant exists for.

    Exact cell traversal cut a real tick to ~27 candidates a ray. At that size
    the dense path's fixed dispatch made it 3.3x slower than the walk, so the
    default must not send an ordinary scene down it.
    """
    rng = np.random.default_rng(13)
    brushes = [box_brush("b%d" % i, tuple(rng.uniform(-2000, 2000, 3)),
                         tuple(rng.uniform(32, 256, 3))) for i in range(600)]
    grid = _world(brushes)

    counts, dense_rays = [], 0
    for _ in range(200):
        start = tuple(rng.uniform(-1800, 1800, 3))
        end = tuple(rng.uniform(-1800, 1800, 3))
        if glm.length(glm.vec3(*end) - glm.vec3(*start)) < 0.001:
            continue
        counts.append(_candidate_count(grid, start, end))
        dense_rays += _path_taken(grid, start, end)[0] == "dense"

    assert max(counts) < SpatialGrid.LOS_DENSE_MIN_CANDIDATES, (
        "a 600-brush scene reached %d candidates on one ray, at or above the "
        "%d threshold -- either the scene got denser or the constant moved"
        % (max(counts), SpatialGrid.LOS_DENSE_MIN_CANDIDATES))
    assert dense_rays == 0, (
        "%d of %d rays took the dense path in an ordinary scene"
        % (dense_rays, len(counts)))
