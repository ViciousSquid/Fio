"""A mover's travel cannot make the collision grid arbitrarily large.

The grid files a mover or door under every cell its travel can reach, so one
bucket lookup finds it anywhere along the way.  For a ``distance`` of 1e9
(a typo is enough) that was two million buckets along an axis and four
trillion on a diagonal: Play start exhausted memory before the first tick.

Past ``MAX_TRAVEL_CELLS`` the brush is now filed where it stands and marked
off range -- the existing route for a brush that may be anywhere, which every
query adds wherever it looks -- so it still collides along its whole travel.
Below the limit nothing changes, and these tests pin that as well.

Headless; the last test drives a real play session and carries ``qt``.
"""

import pytest

from engine import brush_placement as bp
from engine import spatial_grid
from engine.mover_table import travel_range
from engine.spatial_grid import SpatialGrid
from engine.spatial import cell_range
from tests.helpers.worlds import box_brush


@pytest.fixture(autouse=True)
def _no_marks_left_behind():
    before = dict(bp.off_range)
    yield
    if bp.off_range != before:
        bp.off_range.clear()
        bp.off_range.update(before)
        bp.note_off_range()


def _mover(distance, direction=(1, 0, 0), **props):
    return box_brush("lift", (0.0, 100.0, 300.0), (8, 8, 8), is_mover=True,
                     speed=64.0, distance=distance, direction=list(direction),
                     **props)


def _cells_holding(grid, brush):
    return {coord for coord, bucket in grid.cells.items()
            if any(b is brush for b in bucket)}


def _expected_travel_cells(brush, cell_size):
    """Every cell of the travel box, worked out independently of populate."""
    lo, hi, _turns, reach = travel_range(brush)
    x0, z0, x1, z1 = cell_range(lo[0] - reach[0], lo[2] - reach[2],
                                hi[0] + reach[0], hi[2] + reach[2], cell_size)
    return {(x, z) for x in range(x0, x1 + 1) for z in range(z0, z1 + 1)}


@pytest.mark.parametrize("distance,direction", [
    (1e9, (1, 0, 0)), (1e12, (0, 0, 1)), (-1e9, (1, 0, 0)),
    (1e9, (1, 0, 1)), (1e15, (1, 0, 1)), (1e300, (1, 0, 0)),
])
def test_an_absurd_travel_is_filed_in_a_bounded_number_of_cells(
        distance, direction):
    grid = SpatialGrid()
    lift = _mover(distance, direction)
    floor = box_brush("floor", (0, -8, 0), (2048, 16, 2048))

    grid.populate([floor, lift])

    held = _cells_holding(grid, lift)
    assert 1 <= len(held) <= 4, (
        "a mover with distance %g should be filed where it stands, not along "
        "its travel; it is in %d cells" % (distance, len(held)))
    assert len(grid.cells) < 100, (
        "the grid has %d buckets for a floor and one mover" % len(grid.cells))
    assert id(lift) in bp.off_range, (
        "a mover not filed for its whole travel must be off range, or it "
        "would not collide once it leaves the cell it started in")
    lo, hi = lift['_travel_filed'][0], lift['_travel_filed'][1]
    assert all(h - l < 16 for l, h in zip(lo, hi)), (
        "the recorded filed range must be the range actually filed (%r..%r), "
        "or the mover table would think the whole travel is covered" % (lo, hi))


def test_a_capped_mover_still_collides_far_along_its_travel():
    grid = SpatialGrid()
    lift = _mover(1e9)
    grid.populate([box_brush("floor", (0, -8, 0), (2048, 16, 2048)), lift])

    # Where it starts, through its own bucket.
    assert grid.overlaps_wall(0.0, 90.0, 300.0, 8.0)
    # Far along: the brush has moved (published by the mover table in play;
    # written directly here) to a cell it was never filed under.
    lift['pos'] = [5.0e6, 100.0, 300.0]
    lift.pop('_aabb_sig', None)
    assert grid.overlaps_wall(5.0e6, 90.0, 300.0, 8.0), (
        "a capped mover was not found by a query at its current position "
        "5e6 units along its travel")
    assert not grid.overlaps_wall(2.5e6, 90.0, 300.0, 8.0), (
        "and it must not be 'found' where it is not")


def test_the_player_s_collider_query_adds_what_is_off_range():
    """An off-range brush may be anywhere, so the player's broad phase is
    handed it wherever it asks -- once, after the cells' own brushes -- as
    every other query of the grid adds it. (A carried model's collision,
    which is in no mover list, is found only this way once it is off range:
    engine.logic_groups.)"""
    import glm
    grid = SpatialGrid()
    lift = _mover(1e9)
    floor = box_brush("floor", (0, -8, 0), (2048, 16, 2048))
    grid.populate([floor, lift])
    assert id(lift) in bp.off_range

    far = grid.get_potential_colliders(glm.vec3(5000, 0, 5000), glm.vec3(5010, 10, 5010))
    assert [b is lift for b in far] == [True], "one cell, holding nothing but it"
    home = grid.get_potential_colliders(glm.vec3(-10, 0, 290), glm.vec3(10, 10, 310))
    assert sum(b is lift for b in home) == 1 and home[0] is floor
    wide = grid.get_potential_colliders(glm.vec3(-600, 0, -600), glm.vec3(600, 10, 600))
    assert sum(b is lift for b in wide) == 1


@pytest.mark.parametrize("distance,direction", [
    (128.0, (0, 1, 0)), (128.0, (1, 0, 0)), (4096.0, (1, 0, 1)),
    (1e5, (1, 0, 0)), (1e6, (1, 0, 0)), (1e6, (0, 0, 1)), (-1e6, (1, 0, 0)),
    (1e7, (1, 0, 0)), (5e4, (1, 0, 1)),
])
def test_travels_up_to_a_million_units_are_filed_exactly_as_before(
        distance, direction):
    grid = SpatialGrid()
    lift = _mover(distance, direction)
    grid.populate([lift])

    expected = _expected_travel_cells(lift, grid.cell_size)
    held = _cells_holding(grid, lift)
    assert held == expected, (
        "distance %g along %r: filed in %d cells, the travel box covers %d"
        % (distance, direction, len(held), len(expected)))
    assert id(lift) not in bp.off_range, (
        "an ordinary travel must not be marked off range")
    lo, hi, turns, reach = travel_range(lift)
    assert lift['_travel_filed'][:3] == (lo, hi, turns), (
        "the filed range must be the whole travel range, unchanged")


def test_a_door_with_an_absurd_distance_is_capped_too():
    grid = SpatialGrid()
    door = box_brush("door", (0.0, 64.0, 0.0), (64, 128, 16), is_door=True,
                     door_speed=96.0, door_distance=1e9, door_direction="east")
    ordinary = box_brush("door2", (900.0, 64.0, 0.0), (64, 128, 16),
                         is_door=True, door_speed=96.0, door_distance=96.0,
                         door_direction="east")
    grid.populate([door, ordinary])

    assert len(grid.cells) < 100, "%d buckets for two doors" % len(grid.cells)
    assert id(door) in bp.off_range and id(ordinary) not in bp.off_range


def test_the_limit_is_generous_enough_for_any_real_travel():
    assert spatial_grid.MAX_TRAVEL_CELLS >= 4 * (1e6 / 512.0), (
        "a million-unit travel along an axis must stay under the limit")


@pytest.mark.qt
def test_play_starts_and_stops_with_a_billion_unit_mover():
    pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    lift = _mover(1e9, start_on=True)
    state.brushes = [box_brush("floor", (0, -8, 0), (2048, 16, 2048)), lift]
    logic = LogicThread(ThreadedGameState(), state)
    try:
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        logic.io_manager = None
        logic.mover_runtime._init_movers()       # state first, as in play
        grid = SpatialGrid()
        grid.populate(state.brushes)
        assert len(grid.cells) < 100, (
            "Play start built %d buckets for one mover" % len(grid.cells))
        assert id(lift) in bp.off_range, (
            "a mover that starts on has its range checked before the grid "
            "exists; the grid must mark it itself")
        for _ in range(30):
            logic.mover_runtime._update_movers(1.0 / 60.0)
    finally:
        logic.mover_runtime._reset_movers()
        logic.stop()
    assert id(lift) not in bp.off_range, (
        "the off-range mark must be forgotten when play stops")
