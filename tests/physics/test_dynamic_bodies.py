"""Engine-level dynamic body physics (engine.physics.PhysicsWorld).

Runs against the real SpatialGrid so the static-cell cache, the batched
floor query and the push response are exercised exactly as in play mode.
"""
from types import SimpleNamespace

import glm

from engine.physics import PhysicsWorld, SpatialGrid

# Oil_Drum_Grey bounds: local Y starts at zero, so the entity origin is the
# point touching the floor and the collision box centre sits half a height up.
DRUM_SIZE = (45.64271, 65.181947, 45.64271)
DRUM_HALF_Y = DRUM_SIZE[1] * 0.5


def _floor(pos=(0.0, -50.0, 0.0), size=(2000.0, 100.0, 2000.0)):
    return {'pos': list(pos), 'size': list(size), 'is_trigger': False}


def _world(*static_brushes):
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(list(static_brushes) or [_floor()])
    return PhysicsWorld(grid)


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
    return SimpleNamespace(pos=list(pos), properties=properties)


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
    return SimpleNamespace(
        pos=glm.vec3(x, 50.0, 0.0),
        velocity=glm.vec3(vx, 0.0, 0.0),
        width=50.0, height=100.0, depth=50.0,
    )


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


def test_mesh_bounds_define_body_shape_relative_to_origin():
    # _mesh_bounds are world-space (built from world-space mesh triangles).
    prop = _prop((10.0, 20.0, 30.0))
    brush = {
        'pos': list(prop.pos),
        'size': [1.0, 1.0, 1.0],
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'mesh',
        '_mesh_bounds': ([5.0, 20.0, 27.0], [15.0, 30.0, 33.0]),
    }

    half, offset = PhysicsWorld._shape_from_brush(prop, brush)

    assert tuple(half.tolist()) == (5.0, 5.0, 3.0)
    assert tuple(offset.tolist()) == (0.0, 5.0, 0.0)


def test_clear_releases_all_body_state():
    world = _world()
    prop = _prop()
    world.rebuild([_drum_brush(prop)])
    world.step(1.0 / 60.0)

    world.clear()

    assert world.get_body(prop) is None
    assert world._position.shape == (0, 3)
    assert not world._static_cells
    world.step(1.0 / 60.0)  # empty world must be a cheap no-op


# ---------------------------------------------------------------------------
# PhysicsBody is a handle over the world's packed rows. Packing the state into
# those arrays must not cost callers the ability to read a body back.
# ---------------------------------------------------------------------------

def test_body_reports_its_shape_from_the_packed_row():
    world = _world()
    prop = _prop()
    prop.pos = [10.0, 20.0, 30.0]
    brush = {
        'pos': list(prop.pos),
        'size': [1.0, 1.0, 1.0],
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'mesh',
        '_mesh_bounds': ([-5.0, 0.0, -3.0], [5.0, 10.0, 3.0]),
    }
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
# `disabled` is authored state that changes at runtime: an I/O Disable, or Big
# World parking a cell (which stashes the authored flags and forces this one
# on). The pre-vectorisation simulation skipped disabled bodies outright; the
# move into PhysicsWorld left that check behind in PropSession, so a parked or
# disabled prop went on falling while it was supposed to be dormant.
# ---------------------------------------------------------------------------

def test_a_disabled_body_is_not_integrated():
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)
    prop.properties['disabled'] = True

    for _ in range(60):
        world.step(1.0 / 60.0)

    assert prop.pos[1] == 400.0


def test_a_parked_body_resumes_when_it_is_re_enabled():
    world = _world()
    prop = _prop(pos=(0.0, 400.0, 0.0))
    world.rebuild([_drum_brush(prop)])
    world.wake(prop)

    prop.properties['disabled'] = True
    for _ in range(60):
        world.step(1.0 / 60.0)
    assert prop.pos[1] == 400.0

    prop.properties['disabled'] = False
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
