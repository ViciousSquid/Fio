"""Generic Prop gameplay must not depend on the Tidy plugin."""
from pathlib import Path
from types import SimpleNamespace

from engine.physics import PhysicsWorld
from engine.prop_runtime import PropSession
from plugins.entitybase import Prop


class Grid:
    def raycast_down(self, x, z, from_y):
        return 0.0


class IO:
    def __init__(self):
        self.events = []

    def fire_output(self, entity, name):
        self.events.append((entity, name))


def test_core_prop_pickup_drop_without_plugins():
    prop = Prop(pos=[0, 40, 30], properties={
        'physics_enabled': True,
        'no_collision': False,
        'drop_angular_velocity': [10, 0, 0],
    })
    io = IO()

    grid = Grid()
    physics = PhysicsWorld(grid)
    physics.register_body(
        prop,
        {
            'pos': [0, 0, 30],
            'size': [32, 32, 32],
            '_physics_body': True,
            '_physics_entity': prop,
            '_collision_mode': 'aabb',
        },
    )

    logic = SimpleNamespace(
        things=[prop],
        io_manager=io,
        _spatial_grid=grid,
        _physics_world=physics,
        player=SimpleNamespace(
            pos=[0, 0, 0], angle=0.0, pitch=0.0,
            camera_height=40.0,
        ),
        current_hud_message='',
    )
    session = PropSession(logic)
    session.start()

    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert physics.get_body(prop).kinematic
    assert io.events[-1][1] == 'OnPickedUp'

    session.tick(1 / 60, use_pressed=True)
    assert session.held is None
    assert not physics.get_body(prop).kinematic
    assert physics.get_body(prop).awake
    assert 'OnDropped' in [event for _, event in io.events]

    for _ in range(30):
        physics.step(1 / 60)

    assert prop.pos[1] == 0.0
    assert ('OnRest' in [event for _, event in io.events])
    assert prop.properties['rotation'][0] > 0


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
