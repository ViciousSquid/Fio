"""Generic Prop gameplay must not depend on the Tidy plugin."""
from pathlib import Path
from types import SimpleNamespace

from engine.physics import PhysicsWorld, SpatialGrid
from engine.prop_runtime import PropSession
from engine.prop_entity import Prop


class IO:
    def __init__(self):
        self.events = []

    def fire_output(self, entity, name):
        self.events.append((entity, name))

    def names(self):
        return [name for _, name in self.events]


def _floor_grid():
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}])
    return grid


def test_core_prop_pickup_carry_drop_rest_without_plugins():
    spin = 90.0  # degrees per second about X
    prop = Prop(pos=[0.0, 40.0, 30.0], properties={
        'physics_enabled': True,
        'no_collision': False,
        'drop_angular_velocity': [spin, 0.0, 0.0],
    })
    io = IO()
    grid = _floor_grid()
    physics = PhysicsWorld(grid)
    # Origin at the prop's base; 32-unit box centred half a height above it.
    physics.rebuild([{
        'pos': [0.0, 56.0, 30.0],
        'size': [32.0, 32.0, 32.0],
        '_physics_body': True,
        '_physics_entity': prop,
        '_collision_mode': 'aabb',
    }])
    logic = SimpleNamespace(
        things=[prop], io_manager=io,
        _spatial_grid=grid, _physics_world=physics,
        player=SimpleNamespace(pos=[0.0, 0.0, 0.0], angle=0.0, pitch=0.0,
                               camera_height=40.0),
        current_hud_message='',
    )
    session = PropSession(logic)
    session.start()

    # Pick up: the prop is directly ahead at eye height.
    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert physics.get_body(prop).kinematic
    assert io.names()[-1] == 'OnPickedUp'

    # Carry: the prop follows the view; physics must not move it.
    session.tick(1 / 60, use_pressed=False)
    physics.step(1 / 60)
    carried = list(prop.pos)
    assert carried[2] > 30.0
    assert logic.current_hud_message == '[E] Drop'

    # Drop: physics takes over from the carried position, not the home one.
    session.tick(1 / 60, use_pressed=True)
    assert session.held is None
    body = physics.get_body(prop)
    assert not body.kinematic and body.awake
    assert 'OnDropped' in io.names()

    physics.step(1 / 60)
    assert abs(prop.properties['rotation'][0] - spin / 60) < 1e-3
    assert abs(prop.pos[2] - carried[2]) < 1e-3

    for _ in range(60):
        physics.step(1 / 60)
    assert abs(prop.pos[1]) < 1e-4
    assert 'OnRest' in io.names()
    assert not body.awake

    # Stop restores the authored home position and releases callbacks.
    session.stop()
    assert prop.pos == [0.0, 40.0, 30.0]
    assert session.props == []


def test_session_detects_maps_without_props_and_unloads_when_removed():
    prop = Prop(pos=[0, 0, 0])
    logic = SimpleNamespace(things=[prop])
    assert PropSession.has_props(logic.things)
    session = PropSession(logic)
    session.start()

    logic.things.clear()
    assert session.is_empty()
    assert session.props == []
    assert not PropSession.has_props(logic.things)


class CountingThings(list):
    """A thing list that records every full traversal of itself."""

    def __init__(self, items):
        super().__init__(items)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return super().__iter__()


def test_the_liveness_poll_does_not_walk_the_map_every_frame():
    """``is_empty`` runs once per tick from both the logic thread and the
    standalone player; deriving the live set means walking every Thing in the
    map, so it is gated on a cheap fingerprint of the thing list."""
    things = CountingThings([Prop(pos=[0, 0, 0])]
                            + [SimpleNamespace(properties={'type': 'light'})
                               for _ in range(200)])
    session = PropSession(SimpleNamespace(things=things))
    session.start()

    walks_before = things.walks
    for _ in range(PropSession.RESCAN_INTERVAL):
        assert session.is_empty() is False
    assert things.walks == walks_before, (
        "the liveness poll walked the map %d times in %d ticks"
        % (things.walks - walks_before, PropSession.RESCAN_INTERVAL))


def test_the_liveness_poll_still_notices_a_removal_immediately():
    prop, other = Prop(pos=[0, 0, 0]), Prop(pos=[10, 0, 0])
    things = [prop, other]
    session = PropSession(SimpleNamespace(things=things))
    session.start()
    assert session.is_empty() is False

    things.remove(other)
    assert session.is_empty() is False
    assert session.props == [prop], "a removed Prop stayed in the session"

    things.remove(prop)
    assert session.is_empty() is True


def test_a_same_length_swap_is_caught_by_the_periodic_rescan():
    """The fingerprint cannot see a remove-and-add inside one poll interval,
    which is exactly why the rescan is unconditional every N polls."""
    prop = Prop(pos=[0, 0, 0])
    things = [prop]
    logic = SimpleNamespace(things=things)
    session = PropSession(logic)
    session.start()
    session.held = prop

    things[0] = Prop(pos=[0, 0, 0])          # same length, different object
    for _ in range(PropSession.RESCAN_INTERVAL + 1):
        session.is_empty()
    assert session.props == [], "the replaced Prop was never pruned"
    assert session.held is None, "the session kept hold of a Prop that is gone"


def test_prop_has_a_default_billboard_and_2d_menu_entry():
    prop = Prop()
    assert prop.get_sprite_path() == 'assets/sprites/pickup.png'
    source = Path('editor/view_2d.py').read_text()
    assert 'add_prop_action = menu.addAction("Prop")' in source
    assert 'new_thing = Prop(pos=pos_3d)' in source


def test_prop_exposes_mass_and_collision_shape_defaults():
    prop = Prop()
    assert prop.properties['mass'] == 1.0
    assert prop.properties['no_collision'] is True
    assert prop.properties['physics_enabled'] is False
    assert prop.properties['collision_shape'] == 'auto'


def test_aabb_collision_shape_skips_mesh_collision():
    from types import SimpleNamespace
    from engine.logic_thread import LogicThread

    class Builder:
        model_collision_enabled = True

        def __init__(self):
            self.things = [
                SimpleNamespace(
                    pos=[10.0, 20.0, 30.0],
                    properties={
                        'type': 'prop',
                        'model_path': 'Barrel7.obj',
                        'scale': [2.0, 2.0, 2.0],
                        'rotation': [0.0, 0.0, 0.0],
                        'collision_shape': 'aabb',
                        'collision_size': [0.0, 0.0, 0.0],
                        'no_collision': False,
                        'physics_enabled': False,
                    },
                )
            ]

        def _compute_model_bounds(self, model_path):
            assert model_path == 'Barrel7.obj'
            return ([-5.0, 0.0, -3.0], [5.0, 10.0, 3.0])

        def _compute_model_collision_mesh(self, *args):
            raise AssertionError("AABB mode must not build mesh collision")

    brushes = LogicThread._build_model_collision_brushes(Builder())
    assert len(brushes) == 1
    assert brushes[0]['_collision_mode'] == 'aabb'
    assert brushes[0]['size'] == [20.0, 20.0, 12.0]
    assert brushes[0]['pos'] == [10.0, 30.0, 30.0]
