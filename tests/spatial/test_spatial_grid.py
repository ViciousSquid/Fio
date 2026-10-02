"""``engine.physics.SpatialGrid``: the collision/AI broad phase.

This is the grid the player's physics and every MonsterAI query go through, so
what matters is not just that a lookup finds the right brushes but that it can
never *miss* one — a brush dropped from the grid is a wall the player walks
through.  The tests below therefore compare the grid's answers against an
exhaustive scan of the same brush set wherever a reference answer is available.

The other half is membership: ``populate`` rebuilds the grid in place, so a
brush that moved must not still be filed under the cell it left.
"""

import math

import glm
import pytest

from engine.physics import SpatialGrid
from engine.spatial import CELL_SIZE, PARKED_HIDDEN_KEY
from tests.helpers.worlds import box_brush, pillar_grid, room


def _all_solid_brushes_touching(brushes, min_x, min_z, max_x, max_z):
    """Reference answer: every brush whose XZ footprint overlaps the box."""
    out = []
    for brush in brushes:
        pos, size = brush["pos"], brush["size"]
        b_min_x, b_max_x = pos[0] - size[0] * 0.5, pos[0] + size[0] * 0.5
        b_min_z, b_max_z = pos[2] - size[2] * 0.5, pos[2] + size[2] * 0.5
        if b_max_x >= min_x and b_min_x <= max_x and \
                b_max_z >= min_z and b_min_z <= max_z:
            out.append(brush)
    return out


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------

def test_populate_files_every_solid_brush():
    brushes = pillar_grid(4, 4, spacing=600.0)
    grid = SpatialGrid()
    grid.populate(brushes)
    filed = {id(b) for bucket in grid.cells.values() for b in bucket}
    missing = [b["name"] for b in brushes if id(b) not in filed]
    assert not missing, "brushes absent from the grid: %s" % (missing,)


def test_hidden_and_fog_brushes_are_not_solid():
    brushes = [
        box_brush("solid", (0, 0, 0), (64, 64, 64)),
        box_brush("hidden", (100, 0, 0), (64, 64, 64), hidden=True),
        box_brush("fog", (200, 0, 0), (64, 64, 64), is_fog=True),
    ]
    grid = SpatialGrid()
    grid.populate(brushes)
    names = {b["name"] for bucket in grid.cells.values() for b in bucket}
    assert names == {"solid"}, \
        "grid holds %s; only the solid brush should collide" % (sorted(names),)


def test_a_plain_trigger_is_not_solid_but_a_moving_one_is():
    brushes = [
        box_brush("trigger", (0, 0, 0), (64, 64, 64), is_trigger=True),
        box_brush("moving_trigger", (100, 0, 0), (64, 64, 64),
                  is_trigger=True, is_mover=True),
        box_brush("door", (200, 0, 0), (64, 64, 64), is_trigger=True, is_door=True),
    ]
    grid = SpatialGrid()
    grid.populate(brushes)
    names = {b["name"] for bucket in grid.cells.values() for b in bucket}
    assert names == {"moving_trigger", "door"}, (
        "a trigger you can walk through must not be in the collision grid, but "
        "a mover/door must; grid holds %s" % (sorted(names),))


def test_water_is_kept_apart_from_the_solid_brushes():
    brushes = [box_brush("pool", (0, 0, 0), (256, 64, 256), is_water=True)]
    grid = SpatialGrid()
    grid.populate(brushes)
    assert grid.water_brushes == brushes, \
        "water must be filed for swim queries, got %s" % (grid.water_brushes,)
    assert grid.cells == {}, "water is not solid and must not be in a collision cell"


def test_a_brush_parked_by_streaming_stays_in_the_collision_grid():
    """The grid outlives a cell's activation state.

    Rebuilding it mid-play (a model-collision toggle) while a cell is parked
    would otherwise drop those brushes from collision for good.
    """
    parked = box_brush("parked", (0, 0, 0), (64, 64, 64))
    parked["hidden"] = True
    parked[PARKED_HIDDEN_KEY] = False
    grid = SpatialGrid()
    grid.populate([parked])
    assert grid.get_nearby_brushes(0.0, 0.0) == [parked], (
        "a brush parked by the streaming layer was dropped from the collision "
        "grid; it would stay non-solid even after its cell came back")


def test_a_brush_the_mapper_hid_stays_out_even_while_parked():
    brush = box_brush("authored_hidden", (0, 0, 0), (64, 64, 64))
    brush["hidden"] = True
    brush[PARKED_HIDDEN_KEY] = True
    grid = SpatialGrid()
    grid.populate([brush])
    assert grid.cells == {}


def test_mesh_collision_brushes_are_indexed_by_their_mesh_bounds():
    """A model's pseudo-brush has pos/size of the entity, not of its mesh."""
    brush = box_brush("model", (0, 0, 0), (1, 1, 1))
    brush["_collision_mode"] = "mesh"
    brush["_mesh_bounds"] = ([-700.0, 0.0, -700.0], [700.0, 100.0, 700.0])
    grid = SpatialGrid()
    grid.populate([brush])
    assert len(grid.cells) >= 9, (
        "a 1400-unit mesh should span at least 3x3 cells of %d units, it "
        "occupies %d" % (CELL_SIZE, len(grid.cells)))
    assert grid.get_nearby_brushes(600.0, 600.0) == [brush], (
        "a point inside the mesh bounds but outside pos/size found nothing")


def test_repopulating_drops_the_previous_membership():
    """The grid is rebuilt in place; stale membership is the hazard."""
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64))
    grid = SpatialGrid()
    grid.populate([brush])
    old_cell = (0, 0)
    assert brush in grid.cells[old_cell]

    brush["pos"] = [4000.0, 0.0, 4000.0]
    grid.populate([brush])

    assert brush not in grid.cells.get(old_cell, ()), (
        "the brush is still filed under %s after moving to %s - a query there "
        "would collide against a wall that is no longer present"
        % (old_cell, brush["pos"]))
    new_cell = (int(4000 // CELL_SIZE), int(4000 // CELL_SIZE))
    assert brush in grid.cells[new_cell], \
        "the brush is not filed under its new cell %s" % (new_cell,)


def test_clear_removes_solids_and_water_alike():
    grid = SpatialGrid()
    grid.populate([box_brush("a"), box_brush("pool", (500, 0, 0), is_water=True)])
    grid.clear()
    assert grid.cells == {}
    assert grid.water_brushes == []
    assert grid.get_nearby_brushes(0.0, 0.0) == []


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def test_potential_colliders_never_miss_a_brush_an_exhaustive_scan_finds():
    brushes = pillar_grid(6, 6, spacing=300.0)
    grid = SpatialGrid()
    grid.populate(brushes)

    for probe_x in (-800.0, -10.0, 0.0, 250.0, 900.0):
        for probe_z in (-800.0, -10.0, 0.0, 250.0, 900.0):
            lo = glm.vec3(probe_x - 32, 0, probe_z - 32)
            hi = glm.vec3(probe_x + 32, 128, probe_z + 32)
            found = grid.get_potential_colliders(lo, hi)
            expected = _all_solid_brushes_touching(
                brushes, lo.x, lo.z, hi.x, hi.z)
            missed = [b["name"] for b in expected if b not in found]
            assert not missed, (
                "query at (%.0f, %.0f) missed %s; the grid returned %s"
                % (probe_x, probe_z, missed, [b["name"] for b in found]))


def test_potential_colliders_deduplicates_across_cells():
    """A brush spanning cells is referenced from each; it must be returned once."""
    wide = box_brush("wide", (0, 0, 0), (2000.0, 64.0, 2000.0))
    grid = SpatialGrid()
    grid.populate([wide])
    found = grid.get_potential_colliders(glm.vec3(-900, 0, -900),
                                         glm.vec3(900, 64, 900))
    assert found.count(wide) == 1, (
        "a brush spanning %d cells came back %d times"
        % (len(grid.cells), found.count(wide)))


def test_the_single_cell_fast_path_agrees_with_the_multi_cell_path():
    brushes = pillar_grid(3, 3, spacing=100.0)   # all inside one cell
    grid = SpatialGrid()
    grid.populate(brushes)
    one_cell = grid.get_potential_colliders(glm.vec3(-50, 0, -50),
                                            glm.vec3(50, 100, 50))
    # Same region, forced across cells by widening past a boundary and back.
    spanning = grid.get_potential_colliders(glm.vec3(-50, 0, -50),
                                            glm.vec3(50, 100, 50))
    assert one_cell == spanning
    assert set(map(id, one_cell)) <= set(map(id, brushes))


def test_get_nearby_brushes_widens_with_the_radius():
    brushes = [box_brush("near", (0, 0, 0), (64, 64, 64)),
               box_brush("far", (1500, 0, 0), (64, 64, 64))]
    grid = SpatialGrid()
    grid.populate(brushes)
    assert [b["name"] for b in grid.get_nearby_brushes(0.0, 0.0)] == ["near"]
    wide = {b["name"] for b in grid.get_nearby_brushes(0.0, 0.0, radius=2000.0)}
    assert wide == {"near", "far"}, \
        "a 2000-unit radius query found %s" % (sorted(wide),)


def test_get_nearby_brushes_of_an_empty_region_is_empty():
    grid = SpatialGrid()
    grid.populate([box_brush("a")])
    assert grid.get_nearby_brushes(50000.0, 50000.0) == []


def test_raycast_down_finds_the_highest_surface_below_the_start():
    brushes = [
        box_brush("floor", (0, 0, 0), (512, 32, 512)),      # top at y=16
        box_brush("platform", (0, 100, 0), (128, 32, 128)),  # top at y=116
    ]
    grid = SpatialGrid()
    grid.populate(brushes)
    assert grid.raycast_down(0.0, 0.0, start_y=1000.0) == pytest.approx(116.0), \
        "should land on the platform"
    assert grid.raycast_down(0.0, 0.0, start_y=50.0) == pytest.approx(16.0), (
        "starting below the platform must ignore it and land on the floor")
    assert grid.raycast_down(1000.0, 1000.0) is None, \
        "there is nothing under (1000, 1000)"


def test_overlaps_wall_matches_the_box_it_documents():
    """The probe box is margin-wide in XZ and 128 units tall from ``my``."""
    grid = SpatialGrid()
    grid.populate([box_brush("wall", (0, 50, 0), (64, 100, 64))])   # y 0..100

    assert grid.overlaps_wall(0.0, 0.0, 0.0, 16.0) is True
    assert grid.overlaps_wall(200.0, 0.0, 0.0, 16.0) is False, \
        "a probe 200 units to the side must not overlap a 64-wide wall"
    assert grid.overlaps_wall(0.0, 200.0, 0.0, 16.0) is False, (
        "a probe standing at y=200 is entirely above a wall that ends at y=100")


def test_overlaps_wall_agrees_across_the_single_and_multi_cell_paths():
    """The fast path skips de-duplication; it must not skip a brush."""
    wall = box_brush("wall", (CELL_SIZE, 50, 0), (64, 100, 64))
    grid = SpatialGrid()
    grid.populate([wall])
    # A probe straddling the cell boundary takes the multi-cell path.
    assert grid.overlaps_wall(CELL_SIZE, 0.0, 0.0, 16.0) is True
    # Well inside one cell, on the same wall.
    assert grid.overlaps_wall(CELL_SIZE + 10.0, 0.0, 0.0, 4.0) is True


# ---------------------------------------------------------------------------
# Line of sight
# ---------------------------------------------------------------------------

def _intersect_ray_aabb(origin, direction, box_min, box_max):
    t_min, t_max = 0.0, 10000.0
    for i in range(3):
        if abs(direction[i]) < 1e-6:
            if origin[i] < box_min[i] or origin[i] > box_max[i]:
                return False, 0
        else:
            inv = 1.0 / direction[i]
            t1 = (box_min[i] - origin[i]) * inv
            t2 = (box_max[i] - origin[i]) * inv
            t_min = max(t_min, min(t1, t2))
            t_max = min(t_max, max(t1, t2))
            if t_min > t_max:
                return False, 0
    return True, t_min


def test_line_of_sight_is_clear_across_an_empty_room():
    grid = SpatialGrid()
    grid.populate(room(size=2048.0))
    assert grid.has_line_of_sight(glm.vec3(-800, 60, 0), glm.vec3(800, 60, 0),
                                  _intersect_ray_aabb) is True


def test_a_wall_blocks_line_of_sight():
    brushes = room(size=2048.0)
    brushes.append(box_brush("pillar", (0, 64, 0), (128, 256, 128)))
    grid = SpatialGrid()
    grid.populate(brushes)
    assert grid.has_line_of_sight(glm.vec3(-800, 64, 0), glm.vec3(800, 64, 0),
                                  _intersect_ray_aabb) is False, \
        "a 128-unit pillar on the line between the two points did not block it"


def test_a_diagonal_ray_sees_a_brush_straddling_a_cell_boundary():
    """Sampling cell centres along a ray used to step over boundary brushes."""
    blocker = box_brush("blocker", (CELL_SIZE, 64, CELL_SIZE), (96, 256, 96))
    grid = SpatialGrid()
    grid.populate([blocker])
    start = glm.vec3(0, 64, 0)
    end = glm.vec3(2 * CELL_SIZE, 64, 2 * CELL_SIZE)
    assert grid.has_line_of_sight(start, end, _intersect_ray_aabb) is False, (
        "a brush at the cell corner (%.0f, %.0f) was stepped over by the "
        "diagonal ray" % (CELL_SIZE, CELL_SIZE))


def test_a_zero_length_ray_is_always_clear():
    grid = SpatialGrid()
    grid.populate([box_brush("wall", (0, 0, 0), (512, 512, 512))])
    p = glm.vec3(0, 0, 0)
    assert grid.has_line_of_sight(p, p, _intersect_ray_aabb) is True


def test_a_brush_beyond_the_far_end_does_not_block():
    grid = SpatialGrid()
    grid.populate([box_brush("far", (900, 64, 0), (64, 256, 64))])
    assert grid.has_line_of_sight(glm.vec3(0, 64, 0), glm.vec3(400, 64, 0),
                                  _intersect_ray_aabb) is True, (
        "a brush 900 units away blocked a ray that stops at 400")


# ---------------------------------------------------------------------------
# Dense scenes
# ---------------------------------------------------------------------------

def test_a_dense_scene_stays_correct_and_does_not_collapse_into_one_cell():
    brushes = pillar_grid(12, 12, spacing=400.0)
    grid = SpatialGrid()
    grid.populate(brushes)
    assert len(grid.cells) > 1, "144 pillars over 4800 units landed in one cell"
    largest = max(len(bucket) for bucket in grid.cells.values())
    assert largest < len(brushes), (
        "one cell holds %d of %d brushes - the grid is not partitioning"
        % (largest, len(brushes)))
    # Every brush is still findable at its own position.
    for brush in brushes:
        found = grid.get_nearby_brushes(brush["pos"][0], brush["pos"][2])
        assert brush in found, "%s is not findable at its own position" % brush["name"]
