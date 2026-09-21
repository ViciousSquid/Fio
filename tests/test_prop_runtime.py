"""Generic Prop gameplay must not depend on the Tidy plugin."""
from pathlib import Path
from types import SimpleNamespace

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


def test_core_prop_pickup_drop_and_rest_without_plugins():
    prop = Prop(pos=[0, 40, 30], properties={'physics_enabled': True,
                                             'drop_angular_velocity': [10, 0, 0]})
    assert prop.properties['no_collision'] is True

    # Persisted props may intentionally keep physics and collision different.
    prop.properties['no_collision'] = False
    io = IO()
    logic = SimpleNamespace(
        things=[prop], io_manager=io, _spatial_grid=Grid(),
        player=SimpleNamespace(pos=[0, 0, 0], angle=0.0, pitch=0.0,
                               camera_height=40.0), current_hud_message='',
    )
    session = PropSession(logic)
    session.start()

    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert io.events[-1][1] == 'OnPickedUp'

    session.tick(1 / 60, use_pressed=True)
    assert session.held is None
    assert 'OnDropped' in [event for _, event in io.events]
    for _ in range(30):
        session.tick(1 / 60, use_pressed=False)
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


def test_physics_prop_is_pushable_by_player():
    prop = Prop(
        pos=[0, 0, 0],
        properties={
            'physics_enabled': True,
            'no_collision': False,
            'mass': 1.0,
        },
    )
    brush = {
        'pos': [0, 25, 0],
        'size': [40, 50, 40],
        '_prop_entity': prop,
        '_dynamic_prop': True,
        '_collision_mode': 'aabb',
    }

    class Grid:
        def raycast_down(self, x, z, from_y):
            return 0.0

        def get_potential_colliders(self, player_min, player_max):
            return []

    class Vec:
        def __init__(self, x=0.0, y=0.0, z=0.0):
            self.x = x
            self.y = y
            self.z = z

    logic = SimpleNamespace(
        things=[prop],
        _model_collision_brushes=[brush],
        _spatial_grid=Grid(),
        player=SimpleNamespace(
            pos=Vec(-30.0, 0.0, 0.0),
            velocity=Vec(120.0, 0.0, 0.0),
            width=50.0,
            height=100.0,
            depth=50.0,
            physics_enabled=True,
        ),
        io_manager=IO(),
        current_hud_message='',
    )

    session = PropSession(logic)
    session.start()
    session.tick(1 / 60, use_pressed=False)

    assert prop.pos[0] > 0.0
    assert session.moving[id(prop)]['velocity_x'] > 0.0


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
