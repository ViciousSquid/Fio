"""Soma on ground that is not a box top: angled brushes and the terrain.

An angled brush is filed in the spatial grid by its box, but a body does not
stand on the box: it stands on the surface, and the grid is what says where
that is (``SpatialGrid.columns``, ``SpatialGrid.terrain_heights``). A slope
gentle enough to walk is climbed and followed down; a steeper one is in the
way; and gravity pulls along it, against the body's friction.
"""
import numpy as np
import pytest

from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from engine.terrain import Terrain
from tests.helpers.runtime import drive, release
from tests.helpers.worlds import angled_brush, box_brush
from tests.physics.test_dynamic_bodies import DRUM_SIZE, _drum_brush, _prop, _run

WIDTH = DRUM_SIZE[0]
SLIDES = {'friction': 0.0, 'linear_damping': 0.0}
#: Where a drum's leading support point is, ahead of its centre.
REACH = 0.75 * WIDTH * 0.5


def _floor():
    return box_brush('floor', (0, -50, 0), (4000, 100, 4000))


def _ramp(rise, **props):
    """A wedge 400 square: its top climbs along +x from 0 at x = -200, *rise*
    up for every unit across, to ``400 * rise`` at x = 200."""
    top = 400.0 * rise
    normal = np.array([-rise, 1.0, 0.0])
    offset = float(normal @ np.array([-200.0, 0.0, 0.0]) / np.linalg.norm(normal))
    return angled_brush('ramp', pos=(0, top / 2.0, 0), size=(400, top, 400),
                        clip_normal=tuple(normal), clip_offset=offset, **props)


def _surface(x, rise):
    return rise * (x + 200.0)


def _world(*brushes, terrain=None):
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(list(brushes))
    grid.set_terrain(terrain)
    return Soma(grid), grid


def _drop(world, prop, frames=400):
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    _run(world, frames)


# -- the grid's answers -------------------------------------------------------

def test_the_grid_gives_an_angled_brushs_true_surface_not_its_box():
    _world_, grid = _world(_floor(), _ramp(0.5))
    rows = grid._rows_now()
    assert rows.shaped.tolist() == [False, True]
    x = np.array([[-150.0, 0.0, 150.0, 250.0]])
    bottom, top = grid.columns(rows, np.array([1]), x, np.zeros_like(x))
    assert top[0, :3] == pytest.approx([25.0, 100.0, 175.0])   # the slope
    assert rows.hi[1, 1] == pytest.approx(200.0)               # the box it is filed by
    assert bottom[0, :3] == pytest.approx([0.0, 0.0, 0.0])
    assert np.isnan(top[0, 3]) and np.isnan(bottom[0, 3])      # past its end


def test_a_boxs_column_is_its_box():
    _world_, grid = _world(_floor())
    rows = grid._rows_now()
    x = np.array([[0.0, 1999.0, 2001.0]])
    bottom, top = grid.columns(rows, np.array([0]), x, np.zeros_like(x))
    assert top[0, :2].tolist() == [0.0, 0.0] and bottom[0, :2].tolist() == [-100.0, -100.0]
    assert np.isnan(top[0, 2])


def test_a_turned_brushs_column_follows_it():
    """A mover turning is not its box either, and the grid reads it where it
    is now: a slab tipped 45 degrees about z has a sloping top."""
    slab = box_brush('slab', (0, 200, 0), (200, 20, 200), is_mover=True)
    _world_, grid = _world(_floor(), slab)
    rows = grid._rows_now()
    x = np.array([[-50.0, 0.0, 50.0]])
    flat = grid.columns(rows, np.array([1]), x, np.zeros_like(x))[1]
    assert flat[0].tolist() == [210.0, 210.0, 210.0]
    try:
        drive(slab, spin=((0.0, 0.0, 1.0), 45.0))
        rows = grid._rows_now()
        tipped = grid.columns(rows, np.array([1]), x, np.zeros_like(x))[1][0]
        assert tipped[2] - tipped[0] == pytest.approx(100.0, abs=1e-6)   # rises 1 in 1
    finally:
        release(slab)


def test_a_world_without_terrain_has_no_terrain_heights():
    _world_, grid = _world(_floor())
    assert grid.terrain is None
    assert grid.terrain_heights([0.0], [0.0]) is None

    terrain = Terrain(seed=42)
    terrain.enabled, terrain.solid = True, False               # drawn, not solid
    grid.set_terrain(lambda: terrain)
    assert grid.terrain_heights([0.0], [0.0]) is None
    terrain.solid = True                                       # read live
    assert grid.terrain_heights([0.0], [0.0])[0] == terrain.get_height_at_safe(0.0, 0.0)


# -- angled brushes -----------------------------------------------------------

def test_a_body_rests_on_the_slope_not_on_the_box_around_it():
    world, _grid = _world(_floor(), _ramp(0.5))
    prop = _prop((0.0, 300.0, 0.0))                    # friction 0.55 holds on 0.5
    _drop(world, prop)

    # On its highest support point's ground: far below the wedge's box top.
    assert prop.pos[1] == pytest.approx(_surface(REACH, 0.5), abs=0.01)
    assert prop.pos[0] == pytest.approx(0.0, abs=0.01)         # and it stays
    assert world.get_body(prop).awake is False


def test_a_body_slides_where_the_slope_is_steeper_than_its_friction():
    """tan > mu slides; tan <= mu holds. Friction 0.4 on a slope of 0.5."""
    world, _grid = _world(_floor(), _ramp(0.5))
    prop = _prop((0.0, 300.0, 0.0), friction=0.4, linear_damping=0.0)
    _drop(world, prop, frames=900)

    assert prop.pos[0] < -200.0 - WIDTH * 0.5                  # off the foot of it
    assert prop.pos[1] == pytest.approx(0.0, abs=1e-3)         # onto the floor
    assert world.get_body(prop).awake is False                 # where friction stops it


def test_a_rolling_body_rolls_down_a_gentle_slope():
    world, _grid = _world(_floor(), _ramp(0.2))
    ball = _prop((0.0, 300.0, 0.0), rolls=True)                # default friction
    _drop(world, ball, frames=900)

    assert ball.pos[0] < -400.0                                # well out on the floor
    assert ball.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_a_body_going_downhill_stays_on_the_ground():
    """Followed down, not dropped down a step at a time: on the slope every
    tick, so friction and rest apply as they do on the flat."""
    world, _grid = _world(_floor(), _ramp(0.5))
    prop = _prop((150.0, 300.0, 0.0), **SLIDES)
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    _run(world, 40)                                            # down onto it
    index = world._indices[id(prop)]
    airborne = 0
    while prop.pos[0] > -150.0:
        world.step(1.0 / 60.0)
        airborne += not world._grounded[index]
        assert prop.pos[1] == pytest.approx(_surface(prop.pos[0] + REACH, 0.5), abs=0.01)
    assert airborne == 0


def test_a_body_moving_into_a_walkable_slope_climbs_it():
    world, _grid = _world(_floor(), _ramp(0.5))
    prop = _prop((-400.0, 0.0, 0.0), **SLIDES)
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    world.wake(prop, [300.0, 0.0, 0.0])
    highest = 0.0
    for _ in range(90):
        world.step(1.0 / 60.0)
        if prop.pos[0] + REACH > -200.0:
            assert prop.pos[1] == pytest.approx(_surface(prop.pos[0] + REACH, 0.5), abs=0.01)
        highest = max(highest, prop.pos[1])

    assert highest > 50.0                                      # up the slope
    assert world.get_body(prop).velocity[0] < 300.0            # gravity against it


def test_a_slope_too_steep_to_walk_is_in_the_way():
    world, _grid = _world(_floor(), _ramp(2.0))
    prop = _prop((-400.0, 0.0, 0.0), **SLIDES)
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    world.wake(prop, [300.0, 0.0, 0.0])
    _run(world, 120)

    assert prop.pos[0] < -200.0                                # stopped at its foot
    assert prop.pos[1] < 10.0                                  # not carried up it
    assert world.get_body(prop).velocity[0] == 0.0


def test_a_body_passes_under_an_angled_brush_overhead():
    """Its box reaches the floor; its solid does not."""
    arch = _ramp(0.5)
    arch['pos'] = [0.0, arch['pos'][1] + 300.0, 0.0]           # lifted clear
    world, _grid = _world(_floor(), arch)
    prop = _prop((-400.0, 0.0, 0.0), **SLIDES)
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    world.wake(prop, [300.0, 0.0, 0.0])
    _run(world, 180)

    assert prop.pos[0] > 300.0
    assert prop.pos[1] == pytest.approx(0.0, abs=1e-3)


def test_a_body_overhanging_a_ledge_is_not_pushed_off_it():
    """Ground under one side and a drop under the other is an edge, not a
    slope -- even with something angled near enough to ask the question."""
    world, _grid = _world(_floor(), box_brush('step', (0, 50, 600), (200, 100, 200)),
                          angled_brush('near', pos=(60, 300, 600), size=(100, 100, 100)))
    prop = _prop((96.0, 250.0, 600.0), **SLIDES)       # a support point over the edge
    _drop(world, prop)

    assert prop.pos[1] == pytest.approx(100.0, abs=1e-3)       # on the step
    assert prop.pos[0] == pytest.approx(96.0, abs=1e-3)        # and still there


def test_a_body_stood_on_a_slope_in_the_map_is_not_floating_at_play_start():
    world, _grid = _world(_floor(), _ramp(0.5))
    prop = _prop((0.0, _surface(REACH, 0.5), 0.0))
    rested = []
    world.rebuild([_drum_brush(prop)])
    world.set_rest_callback(prop, rested.append)
    _run(world, 30)

    assert world.get_body(prop).awake is False
    assert rested == []


# -- the terrain --------------------------------------------------------------

@pytest.fixture
def terrain():
    terrain = Terrain(seed=42)
    terrain.enabled = terrain.solid = True
    return terrain


def test_a_body_lands_on_the_terrain(terrain):
    world, _grid = _world(box_brush('far', (9000, 0, 9000), (10, 10, 10)), terrain=terrain)
    prop = _prop((100.0, 900.0, 200.0), friction=1.0)
    _drop(world, prop, frames=900)

    x, _y, z = prop.pos
    ground = max(terrain.get_height_at_safe(x + dx, z + dz)
                 for dx, dz in ((0, 0), (-REACH, 0), (REACH, 0), (0, -REACH), (0, REACH)))
    assert prop.pos[1] == pytest.approx(ground, abs=0.05)
    assert world.get_body(prop).awake is False


def test_a_body_falls_through_terrain_that_is_not_solid(terrain):
    terrain.solid = False
    world, _grid = _world(box_brush('far', (9000, 0, 9000), (10, 10, 10)), terrain=terrain)
    prop = _prop((100.0, 900.0, 200.0))
    _drop(world, prop, frames=300)

    assert prop.pos[1] < terrain.get_height_at_safe(100.0, 200.0) - 500.0


def test_a_rolling_body_rolls_downhill_on_the_terrain(terrain):
    world, _grid = _world(box_brush('far', (9000, 0, 9000), (10, 10, 10)), terrain=terrain)
    ball = _prop((100.0, 900.0, 200.0), rolls=True)
    world.rebuild([_drum_brush(ball)])
    world.wake(ball)
    index = world._indices[id(ball)]
    while not world._grounded[index]:
        world.step(1.0 / 60.0)
    touched_down = list(ball.pos)
    lowest = touched_down[1]
    for _ in range(900):
        world.step(1.0 / 60.0)
        lowest = min(lowest, ball.pos[1])
        here = terrain.get_height_at_safe(ball.pos[0], ball.pos[2])
        assert here - 0.05 <= ball.pos[1] <= here + 20.0       # on it throughout

    moved = np.hypot(ball.pos[0] - touched_down[0], ball.pos[2] - touched_down[2])
    assert moved > 5.0                                         # it rolled
    assert lowest < touched_down[1] - 1.0                      # downhill


def test_the_higher_of_a_floor_and_the_terrain_is_what_a_body_stands_on(terrain):
    height = terrain.get_height_at_safe(100.0, 200.0)
    deck = box_brush('deck', (100, height + 190.0, 200), (400, 20, 400))
    world, _grid = _world(deck, terrain=terrain)
    prop = _prop((100.0, 900.0, 200.0))
    _drop(world, prop)

    assert prop.pos[1] == pytest.approx(height + 200.0, abs=1e-3)   # the deck's top
