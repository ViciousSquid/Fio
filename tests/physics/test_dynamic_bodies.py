"""Engine-level dynamic body physics (engine.physics.soma.Soma).

Runs against the real SpatialGrid so the static-cell cache, the batched
floor query and the push response are exercised exactly as in play mode.
"""
import glm
import pytest

from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from engine.player import Player
from engine.prop_entity import Prop
from engine.spatial import set_authored_flag, set_live_flag

# Oil_Drum_Grey bounds: local Y starts at zero, so the entity origin is the
# point touching the floor and the collision box centre sits half a height up.
DRUM_SIZE = (45.64271, 65.181947, 45.64271)
DRUM_HALF_Y = DRUM_SIZE[1] * 0.5


def _floor(pos=(0.0, -50.0, 0.0), size=(2000.0, 100.0, 2000.0)):
    return {'pos': list(pos), 'size': list(size), 'is_trigger': False}


def _world(*static_brushes):
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(list(static_brushes) or [_floor()])
    return Soma(grid)


def _prop(pos=(0.0, 0.0, 0.0), **props):
    properties = {
        'physics_enabled': True,
        'no_collision': False,
        'mass': 1.0,
        'gravity': True,
        'friction': 0.55,
        'linear_damping': 0.08,
        'rotation': [0.0, 0.0, 0.0],
        'drop_angular_velocity': [0.0, 0.0, 0.0],
    }
    properties.update(props)
    return Prop(pos=list(pos), properties=properties)


def _drum_brush(prop):
    """Body brush for a drum whose origin is at its base."""
    x, y, z = prop.pos
    return {
        'pos': [x, y + DRUM_HALF_Y, z],
        'size': list(DRUM_SIZE),
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'aabb',
    }


def _player(x=-30.0, vx=120.0):
    player = Player(x, 0.0)
    player.pos = glm.vec3(x, 50.0, 0.0)
    player.velocity = glm.vec3(vx, 0.0, 0.0)
    # Preserve the exact authored collision dimensions of the old fixture,
    # while the object itself is now the production Player.
    player.width = 50.0
    player.height = 100.0
    player.depth = 50.0
    return player


def _run(world, frames, player=None):
    for _ in range(frames):
        world.step(1.0 / 60.0, player)


def test_push_scales_with_player_velocity_and_mass():
    world = _world()
    light = _prop(mass=1.0)
    heavy = _prop((100.0, 0.0, 0.0), mass=10.0)
    world.rebuild([_drum_brush(light), _drum_brush(heavy)])

    world.step(1.0 / 60.0, _player())

    assert light.pos[0] > 0.0
    assert world.get_body(light).velocity[0] > world.get_body(heavy).velocity[0]


def test_resting_body_stays_on_floor_and_sleeps():
    world = _world()
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    _run(world, 120)

    body = world.get_body(prop)
    assert abs(prop.pos[1]) < 1e-5
    assert body.awake is False
    assert body.velocity == [0.0, 0.0, 0.0]


def test_dropped_body_lands_even_when_a_step_crosses_the_floor():
    world = _world()
    prop = _prop((0.0, 120.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    _run(world, 120)

    assert abs(prop.pos[1]) < 1e-5
    assert world.get_body(prop).awake is False


def test_pushing_along_floor_does_not_bounce_onto_low_wall():
    # The wall top is inside the drum's vertical span but above its previous
    # bottom, so it must not be treated as a floor.
    wall = {'pos': [20.0, 15.0, 0.0], 'size': [40.0, 30.0, 200.0], 'is_trigger': False}
    world = _world(_floor(), wall)
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    _run(world, 30, _player())

    assert abs(prop.pos[1]) < 1e-5
    assert prop.pos[0] > 0.0


def test_footprint_is_supported_across_a_floor_seam():
    left = _floor((-50.0, -25.0, 0.0), (98.0, 50.0, 200.0))
    right = _floor((50.0, -25.0, 0.0), (98.0, 50.0, 200.0))
    world = _world(left, right)
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    _run(world, 30)

    assert abs(prop.pos[1]) < 1e-5


def test_ground_friction_is_coulomb_and_stops_a_sliding_body():
    world = _world()
    prop = _prop(friction=0.55)
    world.rebuild([_drum_brush(prop)])
    body = world.get_body(prop)
    speed = 100.0
    body.velocity = [speed, 0.0, 0.0]
    body.awake = True

    _run(world, 30)

    # Stopping distance for deceleration mu*g is v^2 / (2*mu*g); allow a
    # margin for the discrete 60 Hz integration.
    ideal = speed * speed / (2.0 * 0.55 * abs(world.gravity))
    assert abs(prop.pos[1]) < 1e-5
    assert body.velocity == [0.0, 0.0, 0.0]
    assert ideal * 0.8 < prop.pos[0] < ideal * 1.2


def test_no_collision_body_still_falls():
    world = _world()
    prop = _prop((0.0, 40.0, 0.0), no_collision=True)
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    world.step(1.0 / 60.0)

    assert prop.pos[1] < 40.0


def _mesh_body_brush(prop, lo, hi):
    """A Collision Shape: Mesh body brush whose triangles' bounds are
    *lo*..*hi*, carrying the stale ``size`` the collision runtime gives one:
    the bounds are its box (BODY_BRUSH ``_mesh_bounds``)."""
    return {
        'pos': list(prop.pos),
        'size': [1.0, 1.0, 1.0],
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'mesh',
        '_mesh_bounds': (list(lo), list(hi)),
    }


def test_the_mesh_bounds_define_body_shape_relative_to_origin():
    # The body is its triangles' box, relative to the entity's origin --
    # never the stale size copy.
    prop = _prop((10.0, 20.0, 30.0))
    brush = _mesh_body_brush(prop, [5.0, 20.0, 27.0], [15.0, 30.0, 33.0])

    half, offset = Soma._shape_from_brush(prop, brush)

    assert tuple(half.tolist()) == (5.0, 5.0, 3.0)
    assert tuple(offset.tolist()) == (0.0, 5.0, 0.0)


def test_a_box_body_is_its_pos_and_size():
    prop = _prop((10.0, 20.0, 30.0))
    brush = {'pos': [10.0, 24.0, 30.0], 'size': [8.0, 8.0, 4.0],
             '_physics_body': True, '_physics_entity': prop,
             '_collision_mode': 'aabb'}

    half, offset = Soma._shape_from_brush(prop, brush)

    assert tuple(half.tolist()) == (4.0, 4.0, 2.0)
    assert tuple(offset.tolist()) == (0.0, 4.0, 0.0)


def test_clear_releases_all_body_state():
    world = _world()
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)

    world.clear()

    assert world.get_body(prop) is None
    assert world._position.shape == (0, 3)
    assert world._inactive.shape == (0,)
    assert world._pending_settle.shape == (0,)
    world.step(1.0 / 60.0)  # empty world must be a cheap no-op


# ---------------------------------------------------------------------------
# Simulation tiers: a body beyond live simulation is held, not lost.
# ---------------------------------------------------------------------------

def _stamp(prop, tier):
    """Stamp a simulation tier as a classifier does: written, and journalled."""
    from engine.change_journal import touch
    from engine.spatial import SIM_TIER_KEY
    prop.properties[SIM_TIER_KEY] = tier
    touch(prop)


def test_a_body_in_a_distant_cell_is_held_and_carries_on_when_it_is_near():
    from engine.spatial import TIER_ACTIVE, TIER_DISTANT, TIER_NEAR

    world = _world()
    prop = _prop((0.0, 300.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop, [60.0, 0.0, 0.0])
    _run(world, 5)
    held_at = list(prop.pos)
    held_speed = world.get_body(prop).velocity

    _stamp(prop, TIER_DISTANT)
    _run(world, 120)
    assert list(prop.pos) == held_at                    # not simulated
    assert world.get_body(prop).velocity == held_speed  # nor forgotten
    assert world.get_body(prop).awake is True

    for near in (TIER_ACTIVE, TIER_NEAR):
        _stamp(prop, near)
        before = prop.pos[1]
        _run(world, 2)
        assert prop.pos[1] < before                     # falling again


def test_an_unclassified_world_simulates_everything():
    world = _world()
    prop = _prop((0.0, 300.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    _run(world, 300)
    assert prop.pos[1] == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Contact: a body stops at the wall it meets, and rests only on something.
# ---------------------------------------------------------------------------

def _slider(world, speed, **props):
    """A frictionless drum at the origin, woken at *speed* along +x."""
    prop = _prop(friction=0.0, linear_damping=0.0, **props)
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    world.wake(prop, [speed, 0.0, 0.0])
    return prop


def _wall(face_x, thickness=100.0):
    return _floor((face_x + thickness * 0.5, 200.0, 0.0), (thickness, 400.0, 400.0))


@pytest.mark.parametrize('speed', [90.0, 700.0, 1500.0, 3000.0])
def test_a_body_stops_at_the_wall_not_a_step_short_of_it(speed):
    """Going back to where the step began left a gap as wide as the step:
    25 units at 1500 units a second. The body now stops at the face."""
    world = _world(_floor(), _wall(500.0))
    prop = _slider(world, speed)
    _run(world, 600)

    gap = 500.0 - (prop.pos[0] + DRUM_SIZE[0] * 0.5)
    assert 0.0 < gap <= 2.0 * Soma.CONTACT_SKIN
    assert world.get_body(prop).velocity[0] == 0.0


def test_a_fast_body_does_not_step_over_a_thin_wall():
    """100 units a step, against a drum 45 wide and a wall 16 thick: tested
    only where it ends up, the body was past the wall before it was seen."""
    world = _world(_floor(), _wall(500.0, thickness=16.0))
    prop = _slider(world, 6000.0)
    _run(world, 120)

    assert prop.pos[0] + DRUM_SIZE[0] * 0.5 < 500.0


def test_a_body_against_a_wall_leaves_it_and_slides_along_it():
    world = _world(_floor(), _wall(500.0))
    prop = _slider(world, 1500.0)
    _run(world, 60)
    stopped = prop.pos[0]

    world.wake(prop, [0.0, 0.0, 120.0])        # along the wall
    _run(world, 30)
    assert prop.pos[2] == pytest.approx(60.0, abs=1.0)
    assert prop.pos[0] == pytest.approx(stopped)

    world.wake(prop, [-120.0, 0.0, 0.0])       # away from it
    _run(world, 30)
    assert prop.pos[0] == pytest.approx(stopped - 60.0, abs=1.0)


@pytest.mark.parametrize('top', [0.1, 33.333, -7.77, 1234.567])
def test_a_floor_at_an_awkward_height_is_not_a_wall(top):
    """What a body rests on is not in its way, whatever float32 makes of
    the sum of its height, its offset and the floor's."""
    world = _world(_floor((0.0, top - 50.0, 0.0)))
    prop = _prop((0.0, top + 40.0, 0.0), friction=0.0, linear_damping=0.0)
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    _run(world, 120)
    assert prop.pos[1] == pytest.approx(top, abs=1e-3)

    world.wake(prop, [120.0, 0.0, -60.0])
    _run(world, 60)
    assert prop.pos[0] == pytest.approx(120.0, abs=2.0)
    assert prop.pos[2] == pytest.approx(-60.0, abs=1.0)


@pytest.mark.parametrize('speed', [200.0 + 7.3 * k for k in range(55)])
def test_a_thrown_body_does_not_fall_asleep_at_the_top_of_its_arc(speed):
    """Still for one tick is not at rest: at the top of a throw a body's
    speed passes through zero, and one tick in seven it was slow enough to
    be put to sleep there, hanging in the air."""
    world = _world()
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    world.wake(prop, [0.0, speed, 0.0])
    _run(world, 600)

    assert prop.pos[1] == pytest.approx(0.0, abs=1e-3)
    assert world.get_body(prop).awake is False


def test_a_weightless_body_comes_to_rest_in_the_air():
    world = _world()
    prop = _prop((0.0, 300.0, 0.0), gravity=False, linear_damping=4.0)
    world.rebuild([_drum_brush(prop)])
    world.wake(prop, [0.0, 50.0, 0.0])
    _run(world, 600)

    assert world.get_body(prop).awake is False
    assert prop.pos[1] > 300.0


# ---------------------------------------------------------------------------
# PhysicsBody is a handle over the world's packed rows. Packing the state into
# those arrays must not cost callers the ability to read a body back.
# ---------------------------------------------------------------------------

def test_body_reports_its_shape_from_the_packed_row():
    world = _world()
    prop = _prop()
    prop.pos = [10.0, 20.0, 30.0]
    brush = _mesh_body_brush(prop, [-5.0, 0.0, -3.0], [5.0, 10.0, 3.0])
    body = world.register_body(prop, brush)

    # Bounds (-5,0,-3)..(5,10,3): full extent (10, 10, 6), centre (0, 5, 0);
    # offset is that centre relative to the entity origin (10, 20, 30).
    # Read straight after register_body, before any step: the handle has to
    # pack on demand rather than report not-found defaults.
    assert body.half_extents == (5.0, 5.0, 3.0)
    assert body.size == (10.0, 10.0, 6.0)
    assert body.offset == (-10.0, -15.0, -30.0)


def test_body_reports_its_authored_material():
    world = _world()
    prop = _prop(mass=4.0, friction=0.25, linear_damping=0.5, gravity=False)
    world.rebuild([_drum_brush(prop)])
    body = world.get_body(prop)

    assert body.mass == 4.0
    assert body.friction == 0.25
    assert body.linear_damping == 0.5
    assert body.gravity is False
    assert body.solid is True  # _prop() sets no_collision=False


def test_shape_accessors_are_safe_once_the_body_is_gone():
    world = _world()
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    body = world.get_body(prop)
    world.clear()

    assert body.size == (0.0, 0.0, 0.0)
    assert body.offset == (0.0, 0.0, 0.0)
    assert body.velocity == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Hidden and disabled physics bodies stay dormant. Hide sets `hidden` (not
# `disabled`), and Big World parking sets both flags; neither state may fall,
# rotate, be settled into the floor, or be pushed by the player.
# ---------------------------------------------------------------------------

def test_a_disabled_body_is_not_integrated():
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    set_live_flag(prop, 'disabled', True)

    for _ in range(60):
        world.step(1.0 / 60.0)

    assert prop.pos[1] == 400.0


def test_hidden_and_disabled_sleeping_bodies_cannot_be_pushed():
    for flag in ('hidden', 'disabled'):
        world = _world()
        prop = _prop(**{flag: True})
        world.rebuild([_drum_brush(prop)])
        body = world.get_body(prop)
        assert body.awake is False

        world.step(1.0 / 60.0, _player())
        assert prop.pos[0] == 0.0
        assert body.awake is False

        # The same body becomes pushable when its inactive flag is cleared.
        set_live_flag(prop, flag, False)
        world.step(1.0 / 60.0, _player())
        assert prop.pos[0] > 0.0


def test_an_enabled_sunk_body_is_settled_before_a_player_push():
    world = _world()
    prop = _prop((0.0, -DRUM_HALF_Y, 0.0), disabled=True)
    world.rebuild([_drum_brush(prop)])

    # Consume the one-time startup wake while the body is still disabled.
    # It must remain asleep and in its authored, floor-embedded placement.
    world.step(1.0 / 60.0)
    body = world.get_body(prop)
    assert body.awake is False
    assert prop.pos[1] == pytest.approx(-DRUM_HALF_Y)

    set_authored_flag(prop, 'disabled', False)
    assert body.awake is False  # Enable itself does not wake the body.

    # Player contact is its first wake; settling must precede integration.
    world.step(1.0 / 60.0, _player())

    assert prop.pos[1] == pytest.approx(0.0, abs=1e-4)
    assert prop.pos[0] > 0.0


def test_a_sleeping_hidden_body_is_settled_after_a_platform_moves_under_it():
    platform = _floor((0.0, 360.0, 0.0), (2000.0, 60.0, 2000.0))
    world = _world(platform)
    prop = _prop((0.0, 390.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)
    body = world.get_body(prop)
    assert body.awake is False

    set_authored_flag(prop, 'hidden', True)
    world.step(1.0 / 60.0)
    assert body.awake is False
    assert prop.pos[1] == pytest.approx(390.0)

    # The dormant body does not move while the platform rises into it.
    raised_platform = _floor((0.0, 390.0, 0.0), (2000.0, 60.0, 2000.0))
    world.spatial_grid.populate([raised_platform])
    set_authored_flag(prop, 'hidden', False)
    world.wake(prop)  # same wake performed by the Show I/O handler
    world.step(1.0 / 60.0)

    assert prop.pos[1] == pytest.approx(420.0, abs=1e-4)


def test_a_hidden_floating_body_is_not_woken_on_startup():
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0), hidden=True)
    world.rebuild([_drum_brush(prop)])

    world.step(1.0 / 60.0)

    assert world.get_body(prop).awake is False
    assert not world._pending_settle[world._indices[id(prop)]]
    assert prop.pos[1] == 400.0


def test_rebuild_does_not_reuse_the_previous_world_inactive_snapshot():
    world = _world()
    previous = _prop()
    world.rebuild([_drum_brush(previous)])
    world.step(1.0 / 60.0)  # records an active (False) snapshot

    hidden = _prop(pos=(0.0, -DRUM_HALF_Y, 0.0), hidden=True)
    world.rebuild([_drum_brush(hidden)])
    world.step(1.0 / 60.0)
    assert hidden.pos[1] == pytest.approx(-DRUM_HALF_Y)

    set_live_flag(hidden, 'hidden', False)
    world.wake(hidden)
    world.step(1.0 / 60.0)
    assert hidden.pos[1] == pytest.approx(0.0, abs=1e-4)


def test_a_hidden_sunk_body_is_corrected_on_activation_even_across_repack():
    world = _world()
    # The model origin is at its centre, embedded halfway into the floor.
    prop = _prop(pos=(0.0, -DRUM_HALF_Y, 0.0), hidden=True)
    world.rebuild([_drum_brush(prop)])

    # Rebuild and dormant ticks must not move a hidden body.
    assert prop.pos[1] == pytest.approx(-DRUM_HALF_Y)
    world.step(1.0 / 60.0)
    assert prop.pos[1] == pytest.approx(-DRUM_HALF_Y)

    # Force a dense repack after activation; the transition must survive it.
    set_live_flag(prop, 'hidden', False)
    other = _prop(pos=(400.0, 400.0, 0.0))
    world.register_body(other, _drum_brush(other))
    world.wake(prop)  # repack first, then activate the existing body
    world.step(1.0 / 60.0)

    assert prop.pos[1] == pytest.approx(0.0, abs=1e-4)


def test_a_parked_body_resumes_when_it_is_re_enabled():
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    set_live_flag(prop, 'disabled', True)
    for _ in range(60):
        world.step(1.0 / 60.0)
    assert prop.pos[1] == 400.0

    set_live_flag(prop, 'disabled', False)
    for _ in range(60):
        world.step(1.0 / 60.0)
    assert prop.pos[1] < 400.0


def test_an_enabled_body_still_falls():
    """Control: the guard must not freeze ordinary bodies."""
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    for _ in range(60):
        world.step(1.0 / 60.0)

    assert prop.pos[1] < 400.0


# ---------------------------------------------------------------------------
# Floor queries: two implementations of one answer.
#
# _batch_floor dispatches between a scalar path (one raycast_down per support
# point) and a grouped path (all 5N samples bucketed by grid cell and tested
# with one broadcast per cell). The grouped path is the faster one above
# FLOOR_BATCH_MIN_BODIES active bodies, but "faster" is only acceptable if it
# is also the *same* answer, so these pin equality directly rather than
# trusting the threshold.
# ---------------------------------------------------------------------------

import random

import numpy as np
import pytest


def _scattered_world(n_bodies, seed, spread=1500.0):
    rng = random.Random(seed)
    statics = [
        {'pos': [rng.uniform(-spread, spread), rng.uniform(-60.0, 40.0),
                 rng.uniform(-spread, spread)],
         'size': [rng.uniform(100.0, 400.0), 40.0, rng.uniform(100.0, 400.0)]}
        for _ in range(120)
    ]
    world = _world(*statics)
    brushes = []
    for _ in range(n_bodies):
        prop = _prop(pos=(rng.uniform(-spread, spread),
                          rng.uniform(50.0, 400.0),
                          rng.uniform(-spread, spread)))
        brushes.append(_drum_brush(prop))
    world.rebuild(brushes)
    for brush in brushes:
        world.wake(brush['_physics_entity'])
    world._pack()
    return world


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("n_bodies", [1, 7, 40, 130])
def test_grouped_and_scalar_floor_queries_agree_exactly(n_bodies, seed):
    world = _scattered_world(n_bodies, seed)
    for _ in range(3):  # let bodies reach varied heights first
        world.step(1.0 / 60.0)

    grouped = world._batch_floor_grouped()
    scalar = world._batch_floor_scalar()

    # Exact, not approximate: the grouped path promotes to float64 before the
    # support-point arithmetic precisely so it matches the scalar path bit for
    # bit rather than merely closely.
    assert np.array_equal(grouped, scalar)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("n_bodies", [1, 7, 40, 130])
def test_the_steps_floors_are_the_grids_floors_exactly(n_bodies, seed):
    """A step asks the grid once, about each body's whole move, and reads the
    floors out of that answer. They are the floors a raycast per support
    point finds, bit for bit -- wherever in the move the body is."""
    world = _scattered_world(n_bodies, seed)
    for _ in range(3):
        world.step(1.0 / 60.0)
    rows = np.flatnonzero(world._awake)
    old = world._position[rows].copy()
    ahead = old + np.float32(40.0)                  # a move across cells
    previous = (old + world._offset[rows])[:, 1] - world._half[rows, 1]
    world._index_bodies = lambda: False             # the world's floors only
    pairs = world._world_pairs(rows, old, ahead)

    for at in (old, (old + ahead) * np.float32(0.5), ahead):
        world._position[rows, 0] = at[:, 0]
        world._position[rows, 2] = at[:, 2]
        assert np.array_equal(world._floors_from_pairs(rows, previous, pairs),
                              world._floors_scalar(rows, previous))


def test_grouped_floor_query_reads_mover_heights_live():
    """A platform that moves without the grid being repopulated.

    populate() runs on play-start, not per frame, so a mover's cell membership
    is a snapshot while its height is not: the grid re-derives a mover's box
    when its runtime transform changes, and physics asks the grid, so a prop
    never rides a platform that is no longer there.
    """
    from tests.helpers.runtime import drive, release

    platform = {'pos': [0.0, 0.0, 0.0], 'size': [400.0, 20.0, 400.0],
                'is_mover': True}
    world = _world(platform)
    prop = _prop(pos=(0.0, 300.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    world._pack()

    assert world._batch_floor_grouped()[0] == pytest.approx(10.0)

    try:
        drive(platform, offset=(0.0, 100.0, 0.0))   # rises; grid NOT repopulated
        assert world._batch_floor_grouped()[0] == pytest.approx(110.0)
        assert np.array_equal(world._batch_floor_grouped(),
                              world._batch_floor_scalar())
    finally:
        release(platform)


def test_a_door_that_opens_stops_being_a_wall():
    """Walls are the grid's, live: a door is a wall where it is now, not
    where it stood when play began."""
    from tests.helpers.runtime import drive, release

    door = {'pos': [200.0, 100.0, 0.0], 'size': [20.0, 400.0, 400.0],
            'is_door': True}

    def slide(opened):
        world = _world(_floor(), door)
        prop = _prop(friction=0.0, linear_damping=0.0)
        world.rebuild([_drum_brush(prop)])
        world.step(1.0 / 60.0)
        if opened:
            drive(door, offset=(0.0, 1000.0, 0.0))
        world.wake(prop, [600.0, 0.0, 0.0])
        _run(world, 60)
        return prop.pos[0]

    try:
        assert slide(opened=False) < 200.0
        assert slide(opened=True) > 500.0
    finally:
        release(door)


def test_a_custom_grid_keeps_the_scalar_path():
    """A grid with its own raycast_down cannot be answered from `cells`."""
    class TracingGrid(SpatialGrid):
        def raycast_down(self, x, z, start_y=10000.0):
            return 123.0

    world = Soma(TracingGrid(cell_size=512.0))
    assert world._can_group_floor_queries() is False

    prop = _prop(pos=(0.0, 300.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    world._pack()
    assert world._batch_floor()[0] == pytest.approx(123.0)


# ---------------------------------------------------------------------------
# Rotation integration
# ---------------------------------------------------------------------------

def test_rotation_integrates_for_every_spinning_body():
    world = _world()
    brushes = []
    for i in range(5):
        prop = _prop(pos=(i * 200.0, 0.0, 0.0),
                     rotation=[1.0, 2.0, 3.0],
                     drop_angular_velocity=[10.0, 0.0, -5.0] if i % 2 == 0 else [0.0, 0.0, 0.0])
        brushes.append(_drum_brush(prop))
    world.rebuild(brushes)
    world._pack()

    dt = np.float32(1.0 / 60.0)
    world._integrate_rotation(world._angular_velocity, dt)

    for i, brush in enumerate(brushes):
        rotation = brush['_physics_entity'].properties['rotation']
        if i % 2 == 0:
            expected = (np.float32([1.0, 2.0, 3.0])
                        + np.float32([10.0, 0.0, -5.0]) * dt).tolist()
            assert rotation == expected
        else:
            assert rotation == [1.0, 2.0, 3.0]  # untouched


def test_rotation_survives_a_malformed_authored_value():
    """One bad rotation must not abort the step for every other body."""
    world = _world()
    good = _prop(pos=(0.0, 0.0, 0.0), rotation=[0.0, 0.0, 0.0],
                 drop_angular_velocity=[6.0, 0.0, 0.0])
    bad = _prop(pos=(200.0, 0.0, 0.0), rotation="not a rotation",
                drop_angular_velocity=[6.0, 0.0, 0.0])
    world.rebuild([_drum_brush(good), _drum_brush(bad)])
    world._pack()

    dt = np.float32(1.0 / 60.0)
    world._integrate_rotation(world._angular_velocity, dt)

    assert good.properties['rotation'][0] == pytest.approx(0.1)
    assert bad.properties['rotation'][0] == pytest.approx(0.1)  # restarted at 0
