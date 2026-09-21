"""Engine-level dynamic body physics tests."""
from types import SimpleNamespace

from engine.physics import PhysicsWorld


class Grid:
    def raycast_down(self, x, z, start_y):
        return 0.0

    def get_potential_colliders(self, player_min, player_max):
        return []


class Vec:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = x
        self.y = y
        self.z = z


def _prop(**props):
    defaults = {
        'physics_enabled': True,
        'no_collision': False,
        'mass': 1.0,
        'gravity': True,
        'friction': 0.55,
        'linear_damping': 0.08,
        'rotation': [0.0, 0.0, 0.0],
        'drop_angular_velocity': [0.0, 0.0, 0.0],
    }
    defaults.update(props)
    return SimpleNamespace(pos=[0.0, 0.0, 0.0], properties=defaults)


def _brush(prop, size=(40.0, 40.0, 40.0)):
    return {
        'pos': list(prop.pos),
        'size': list(size),
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'aabb',
    }


def test_engine_physics_pushes_body_by_player_velocity_and_mass():
    grid = Grid()
    world = PhysicsWorld(grid)
    light = _prop(mass=1.0)
    heavy = _prop(mass=10.0)
    light.pos = [0.0, 0.0, 0.0]
    heavy.pos = [100.0, 0.0, 0.0]
    world.register_body(light, _brush(light))
    world.register_body(heavy, _brush(heavy))

    player = SimpleNamespace(
        pos=Vec(-30.0, 0.0, 0.0),
        velocity=Vec(120.0, 0.0, 0.0),
        width=50.0, height=100.0, depth=50.0,
    )
    world.step(1.0 / 60.0, player)

    assert light.pos[0] > 0.0
    assert world.get_body(light).velocity[0] > world.get_body(heavy).velocity[0]




def test_engine_physics_drop_lands_when_body_crosses_floor():
    grid = Grid()
    world = PhysicsWorld(grid)
    prop = _prop()
    # Entity origin at the barrel's base; collision box is centred 32.59
    # units above the origin, matching the Oil_Drum_Grey bounds.
    prop.pos = [0.0, 120.0, 0.0]
    body_brush = _brush(prop, (45.64271, 65.181947, 45.64271))
    body_brush['pos'] = [0.0, 152.5909735, 0.0]

    world.rebuild([body_brush])
    world.wake(prop)

    for _ in range(120):
        world.step(1.0 / 60.0)

    # The downward ray must catch the floor even on the frame where the
    # integrated position has crossed it.
    assert abs(prop.pos[1]) < 1e-5
    assert world.get_body(prop).awake is False


def test_engine_physics_does_not_bounce_when_pushing_along_floor():
    from engine.physics import SpatialGrid

    grid = SpatialGrid(cell_size=512.0)
    floor = {
        'pos': [0.0, -50.0, 0.0],
        'size': [1000.0, 100.0, 1000.0],
        'is_trigger': False,
    }
    wall = {
        'pos': [20.0, 15.0, 0.0],
        'size': [40.0, 30.0, 200.0],
        'is_trigger': False,
    }
    grid.populate([floor, wall])

    world = PhysicsWorld(grid)
    prop = _prop()
    # Barrel7/Oil_Drum-style bounds: local Y starts at zero, so the entity
    # origin is the point touching the floor.
    prop.pos = [0.0, 0.0, 0.0]
    body_brush = _brush(prop, (45.64271, 65.181947, 45.64271))
    body_brush['pos'] = [0.0, 32.5909735, 0.0]
    world.rebuild([body_brush])
    world.wake(prop)

    player = SimpleNamespace(
        pos=Vec(-30.0, 0.0, 0.0),
        velocity=Vec(120.0, 0.0, 0.0),
        width=50.0,
        height=100.0,
        depth=50.0,
    )

    for _ in range(30):
        world.step(1.0 / 60.0, player)

    # The wall top is inside the barrel's vertical span, but above its
    # previous bottom, so it must not be treated as a floor. The actual floor
    # remains in contact while the barrel is pushed horizontally.
    assert abs(prop.pos[1]) < 1e-5
    assert prop.pos[0] > 0.0



def test_engine_physics_uses_footprint_support_across_floor_seam():
    from engine.physics import SpatialGrid

    grid = SpatialGrid(cell_size=512.0)
    left_floor = {
        'pos': [-50.0, -25.0, 0.0],
        'size': [98.0, 50.0, 200.0],
        'is_trigger': False,
    }
    right_floor = {
        'pos': [50.0, -25.0, 0.0],
        'size': [98.0, 50.0, 200.0],
        'is_trigger': False,
    }
    grid.populate([left_floor, right_floor])

    world = PhysicsWorld(grid)
    prop = _prop()
    # The centre is over a 2-unit seam, but most of the barrel footprint is
    # still supported by solid floor on either side.
    prop.pos = [0.0, 0.0, 0.0]
    body_brush = _brush(prop, (45.64271, 65.181947, 45.64271))
    body_brush['pos'] = [0.0, 32.5909735, 0.0]
    world.rebuild([body_brush])
    world.wake(prop)

    for _ in range(30):
        world.step(1.0 / 60.0)

    assert abs(prop.pos[1]) < 1e-5


def test_engine_physics_body_lands_on_floor_and_sleeps():
    grid = Grid()
    world = PhysicsWorld(grid)
    prop = _prop()
    prop.pos = [0.0, 40.0, 0.0]
    world.register_body(prop, _brush(prop, (40.0, 80.0, 40.0)))
    world.wake(prop)

    for _ in range(120):
        world.step(1.0 / 60.0)

    body = world.get_body(prop)
    assert prop.pos[1] == 40.0
    assert body.awake is False
    assert body.velocity == [0.0, 0.0, 0.0]


def test_engine_physics_allows_physics_without_solid_collision():
    grid = Grid()
    world = PhysicsWorld(grid)
    prop = _prop(no_collision=True)
    prop.pos = [0.0, 40.0, 0.0]
    world.register_body(prop, _brush(prop, (40.0, 80.0, 40.0)))
    world.wake(prop)

    world.step(1.0 / 60.0)

    assert prop.pos[1] < 40.0


def test_engine_physics_uses_mesh_bounds_for_dynamic_body_shape():
    grid = Grid()
    world = PhysicsWorld(grid)
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

    assert body.size == (10.0, 10.0, 6.0)
    assert body.offset == (0.0, -15.0, -3.0)
