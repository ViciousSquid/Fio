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


def test_the_registry_is_derived_from_the_authoritative_thing_list():
    """PropSession is the Prop registry; the thing list is still the world."""
    prop, light = Prop(pos=[0, 0, 0]), SimpleNamespace(properties={'type': 'light'})
    logic = SimpleNamespace(things=[prop, light])
    session = PropSession(logic)
    session.start()

    assert session.props == [prop]
    assert session.by_id(id(prop)) is prop
    assert session.by_id(id(light)) is None


def test_a_rebuild_adopts_a_new_prop_without_disturbing_the_others():
    """A spawn elsewhere in the map must not reset a Prop already registered."""
    settled = Prop(pos=[0, 0, 0])
    logic = SimpleNamespace(things=[settled])
    session = PropSession(logic)
    session.start()

    settled.pos = [10.0, 20.0, 30.0]          # it has moved since it was adopted
    home = list(settled.properties['_prop_home_pos'])

    spawned = Prop(pos=[100, 0, 0])
    logic.things.append(spawned)
    session.rebuild()

    assert session.props == [settled, spawned]
    assert settled.properties['_prop_home_pos'] == home, (
        "adopting a new Prop re-homed one that was already registered")
    assert spawned.properties['_prop_home_pos'] == [100.0, 0.0, 0.0]


def test_a_rebuild_releases_a_prop_that_left_the_world():
    prop, other = Prop(pos=[0, 0, 0]), Prop(pos=[10, 0, 0])
    logic = SimpleNamespace(things=[prop, other])
    session = PropSession(logic)
    session.start()
    session.held = other

    logic.things.remove(other)
    session.rebuild()

    assert session.props == [prop]
    assert session.by_id(id(other)) is None
    assert session.held is None, "the session kept hold of a Prop that is gone"
    assert '_prop_home_pos' not in other.properties, (
        "a released Prop kept the session's authored state")


def test_an_empty_registry_is_a_valid_state():
    """A map with no Props still has a session; it just has nothing in it."""
    logic = SimpleNamespace(things=[SimpleNamespace(properties={'type': 'light'})])
    session = PropSession(logic)
    session.start()
    assert session.props == []
    session.tick(1 / 60.0, use_pressed=True)      # must not raise


def test_is_prop_is_the_one_type_contract():
    """Every tier decides what a Prop is the same way: the serialised type."""
    assert PropSession.is_prop(Prop(pos=[0, 0, 0])) is True
    assert PropSession.is_prop(SimpleNamespace(properties={'type': 'monster'})) is False
    assert PropSession.is_prop(SimpleNamespace()) is False


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
