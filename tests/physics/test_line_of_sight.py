"""Line of sight: the dense path must answer exactly what the scalar one does.

``SpatialGrid.has_line_of_sight`` has two ways through the brushes a ray's
cells hold. The per-brush path deduplicates them through a set of ``id()``,
reads each one's AABB from its dict and runs the slab test in Python; the dense
path addresses them as :class:`~engine.render_table.RenderTable` rows, gathers
the transform columns and tests all of them at once.

They are one answer or the change moved a wall. The tests here drive both over
the same grid and compare, with the cases most likely to separate them:

* **grazing rays**, because the two derive the AABB differently -- the scalar
  path through ``glm.vec3``, which is float32 -- and the slab test is a
  comparison, so a difference of 3e-04 is enough to decide one;
* **mesh-collision brushes**, because the grid files those by their mesh bounds
  while the narrow phase tests pos/size, so the cell a brush is *in* and the
  box that is *tested* are deliberately different things;
* **movers**, whose transform the projection holds in a column the logic thread
  rewrites per tick.
"""

import numpy as np
import pytest

pytest.importorskip("glm")

import glm                                                   # noqa: E402

from engine.physics import SpatialGrid                       # noqa: E402
from engine.render_table import RenderTable                  # noqa: E402
from tests.helpers.worlds import box_brush                   # noqa: E402


def _intersect_ray_aabb(start, direction, b_min, b_max):
    """The callback the scalar path keeps for API compatibility."""
    raise AssertionError("the inlined slab test should be used, not this")


def _world(brushes):
    table = RenderTable()
    table.sync(brushes, 1)
    grid = SpatialGrid()
    grid.populate(brushes)
    return grid, table


def _both(grid, table, start, end):
    """(scalar answer, dense answer) for one ray."""
    scalar = grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end),
                                    _intersect_ray_aabb, None)
    dense = grid.has_line_of_sight(glm.vec3(*start), glm.vec3(*end),
                                   _intersect_ray_aabb, table)
    return scalar, dense


def _agree(grid, table, start, end, what):
    scalar, dense = _both(grid, table, start, end)
    assert scalar == dense, (
        "%s: the per-brush path said %s and the dense path said %s for the ray "
        "%s -> %s" % (what, scalar, dense, start, end))
    return scalar


# ---------------------------------------------------------------------------
# The projection itself
# ---------------------------------------------------------------------------

def test_the_projection_addresses_every_brush_in_the_grid():
    brushes = [box_brush("w%d" % i, (i * 200.0, 0.0, 0.0)) for i in range(5)]
    grid, table = _world(brushes)

    slots = grid.cell_slots(table)

    assert slots is not None
    addressed = sorted({int(s) for arr in slots.values() for s in arr})
    assert addressed == sorted(table.slot_of_id[b["id"]] for b in brushes)


def test_a_brush_the_table_cannot_address_keeps_the_scalar_path():
    """Fail-safe: an unaddressable brush would be a missing occluder."""
    brushes = [box_brush("wall", (0.0, 0.0, 0.0))]
    grid, table = _world(brushes)
    brushes[0]["id"] = "not-in-the-table"
    grid.populate(brushes)

    assert grid.cell_slots(table) is None


def test_the_failed_attempt_is_not_repeated_every_ray(monkeypatch):
    brushes = [box_brush("wall", (0.0, 0.0, 0.0))]
    grid, table = _world(brushes)
    brushes[0]["id"] = "not-in-the-table"
    grid.populate(brushes)

    builds = []
    real = SpatialGrid._build_cell_slots
    monkeypatch.setattr(SpatialGrid, "_build_cell_slots",
                        lambda self, m: builds.append(1) or real(self, m))
    for _ in range(20):
        grid.cell_slots(table)
    assert len(builds) == 1, (
        "rebuilt %d times; a grid the table cannot address must be asked once "
        "per generation, not once per ray" % len(builds))


def test_populate_drops_the_projection():
    brushes = [box_brush("wall", (0.0, 0.0, 0.0))]
    grid, table = _world(brushes)
    first = grid.cell_slots(table)
    assert first is not None

    grid.populate(brushes)
    assert grid._cell_slots is None, (
        "the projection outlived the buckets it was derived from")
    assert grid.cell_slots(table) is not None


def test_a_reconciled_table_rebuilds_the_projection():
    """Slots are addresses valid within one generation."""
    brushes = [box_brush("a", (0.0, 0.0, 0.0)), box_brush("b", (600.0, 0.0, 0.0))]
    grid, table = _world(brushes)
    grid.cell_slots(table)
    generation = table.generation

    # Reorder: same brushes, different rows.
    brushes.reverse()
    table.sync(brushes, 2)
    assert table.generation != generation

    slots = grid.cell_slots(table)
    for coord, bucket in grid.cells.items():
        for i, brush in enumerate(bucket):
            assert table.ids[int(slots[coord][i])] == brush["id"], (
                "a slot still points at the row the brush had before the "
                "table reconciled")


def test_the_projection_is_built_once_while_nothing_changes(monkeypatch):
    brushes = [box_brush("w%d" % i, (i * 200.0, 0.0, 0.0)) for i in range(5)]
    grid, table = _world(brushes)

    builds = []
    real = SpatialGrid._build_cell_slots
    monkeypatch.setattr(SpatialGrid, "_build_cell_slots",
                        lambda self, m: builds.append(1) or real(self, m))
    for _ in range(50):
        grid.cell_slots(table)
    assert len(builds) == 1


def test_a_torn_read_of_the_columns_falls_back_rather_than_raising():
    """The one failure mode the scalar path does not have.

    Line of sight runs on the AI thread and reads two columns of a table the
    logic thread owns. Reading a mover's transform mid-write is the race this
    path has always had -- the scalar path reads ``brush['pos']`` live for the
    same reason, deliberately. What is new is that there are two arrays: a
    reconcile landing between the reads would reallocate them, and a slot valid
    for one could be past the end of the other.

    That cannot be made atomic without synchronising against the render pass,
    so it is answered by the scalar path instead -- the same direction as a
    brush the table cannot address.
    """
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (32.0, 256.0, 512.0))]
    grid, table = _world(brushes)
    assert grid.cell_slots(table) is not None

    blocked = grid.has_line_of_sight(glm.vec3(-300, 0, 0), glm.vec3(300, 0, 0),
                                     _intersect_ray_aabb, table)
    # Simulate the reallocation: the columns no longer hold the row the
    # projection was built against.
    table.center = table.center[:0]
    still = grid.has_line_of_sight(glm.vec3(-300, 0, 0), glm.vec3(300, 0, 0),
                                   _intersect_ray_aabb, table)
    assert blocked is False
    assert still == blocked, (
        "a torn read changed the answer instead of falling back to the "
        "per-brush path")


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

def test_a_wall_blocks_both_paths():
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (32.0, 256.0, 512.0))]
    grid, table = _world(brushes)
    assert _agree(grid, table, (-300, 0, 0), (300, 0, 0), "through a wall") is False


def test_a_clear_line_passes_both_paths():
    brushes = [box_brush("wall", (0.0, 0.0, 900.0), (32.0, 256.0, 128.0))]
    grid, table = _world(brushes)
    assert _agree(grid, table, (-300, 0, 0), (300, 0, 0), "clear line") is True


def test_a_ray_that_stops_short_of_the_wall_reaches_neither():
    """`limit = ray_len - 0.1`: a hit past the end is not a hit."""
    brushes = [box_brush("wall", (500.0, 0.0, 0.0), (32.0, 256.0, 256.0))]
    grid, table = _world(brushes)
    assert _agree(grid, table, (0, 0, 0), (100, 0, 0), "ray stops short") is True
    assert _agree(grid, table, (0, 0, 0), (600, 0, 0), "ray reaches") is False


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
    grid, table = _world(brushes)
    edge = size * 0.5
    _agree(grid, table, (-400.0, edge + offset, 0.0), (400.0, edge + offset, 0.0),
           "grazing the top face by %g" % offset)
    _agree(grid, table, (-400.0, 0.0, edge + offset), (400.0, 0.0, edge + offset),
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
    grid, table = _world([wall])

    glm_hi_y = brush_aabb_bounds(wall)[4]
    float64_hi_y = wall["pos"][1] + wall["size"][1] * 0.5
    assert glm_hi_y > float64_hi_y, (
        "this scene no longer separates the two roundings (%r vs %r)"
        % (glm_hi_y, float64_hi_y))

    between = (glm_hi_y + float64_hi_y) * 0.5
    assert float64_hi_y < between < glm_hi_y

    scalar, dense = _both(grid, table, (-4000.0, between, 0.0),
                          (4000.0, between, 0.0))
    assert scalar is False, "the ray should be inside the float32 box"
    assert dense == scalar, (
        "the dense path said %s where the per-brush path said %s -- it is not "
        "reproducing the float32 rounding of brush_aabb_bounds" % (dense, scalar))


def test_an_axis_parallel_ray_agrees():
    """The degenerate branch: parallel to a slab, inside it or rejected."""
    brushes = [box_brush("wall", (0.0, 0.0, 0.0), (64.0, 64.0, 64.0))]
    grid, table = _world(brushes)
    for start, end, what in (
            ((-400, 0, 0), (400, 0, 0), "along +x through the box"),
            ((-400, 200, 0), (400, 200, 0), "along +x above the box"),
            ((0, -400, 0), (0, 400, 0), "along +y through the box"),
            ((0, 0, -400), (0, 0, 400), "along +z through the box"),
            ((-400, 32.0, 0), (400, 32.0, 0), "along +x exactly on the top face")):
        _agree(grid, table, start, end, what)


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
    grid, table = _world(brushes)

    occupied = len(grid.cells)
    assert occupied > 1, (
        "the mesh bounds should span several cells; got %d" % occupied)
    assert _agree(grid, table, (-300, 0, 0), (300, 0, 0), "mesh brush, through") is False
    assert _agree(grid, table, (-300, 0, 600), (300, 0, 600),
                  "mesh brush, past its pos/size box but inside its mesh bounds") is True


def test_a_moved_mover_is_seen_live_by_both_paths():
    lift = box_brush("lift", (0.0, 0.0, 0.0), (64.0, 256.0, 256.0), is_mover=True)
    brushes = [lift]
    grid, table = _world(brushes)
    assert _agree(grid, table, (-300, 0, 0), (300, 0, 0), "mover in the way") is False

    lift["pos"] = [0.0, 900.0, 0.0]
    table.refresh_transforms(brushes, [table.slot_of_id[lift["id"]]])
    assert _agree(grid, table, (-300, 0, 0), (300, 0, 0), "mover lifted away") is True


def test_the_two_paths_agree_over_a_random_sweep():
    """Breadth, against a scene with enough brushes to fill several cells."""
    rng = np.random.default_rng(11)
    brushes = []
    for i in range(120):
        pos = rng.uniform(-1200, 1200, 3)
        size = rng.uniform(16, 320, 3)
        brushes.append(box_brush("b%d" % i, tuple(pos), tuple(size)))
    grid, table = _world(brushes)

    disagreements = []
    blocked = 0
    for _ in range(400):
        start = tuple(rng.uniform(-1500, 1500, 3))
        end = tuple(rng.uniform(-1500, 1500, 3))
        scalar, dense = _both(grid, table, start, end)
        if scalar != dense:
            disagreements.append((start, end, scalar, dense))
        blocked += not scalar

    assert not disagreements, (
        "%d of 400 rays disagreed; first %s" % (len(disagreements),
                                                disagreements[0]))
    assert 20 < blocked < 380, (
        "only %d of 400 rays were blocked -- the sweep is not exercising both "
        "answers" % blocked)
