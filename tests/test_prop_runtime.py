"""Generic Prop gameplay must not depend on the Tidy plugin."""
from pathlib import Path

import glm
import pytest

pytest.importorskip("PyQt5", reason="Prop runtime tests exercise the real LogicThread")

from editor.editor_state import EditorState                    # noqa: E402
from editor.things import Light                                 # noqa: E402
from engine.logic_thread import LogicThread                     # noqa: E402
from engine.physics import PhysicsWorld, SpatialGrid            # noqa: E402
from engine.player import Player                                 # noqa: E402
from engine.prop_entity import Prop                              # noqa: E402
from engine.prop_runtime import PropSession                      # noqa: E402
from engine.threaded_game_state import ThreadedGameState          # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def real_logic():
    logics = []

    def make(things=(), brushes=(), grid=None, physics=None):
        state = EditorState()
        state.things = list(things)
        state.brushes = list(brushes)

        logic = LogicThread(ThreadedGameState(), state)
        logics.append(logic)

        player = Player(0.0, 0.0, angle=0.0)
        player.pos = glm.vec3(0.0, 0.0, 0.0)
        player.pitch = 0.0
        logic.player_runtime.player = player
        logic.session_runtime.spatial_grid = grid
        logic.session_runtime.physics_world = physics

        events = []
        io = logic.io_manager
        fire = io.fire_output

        def recording_fire(source, output, value=None, activator_entity=None):
            events.append((source, output))
            return fire(source, output, value=value, activator_entity=activator_entity)

        io.fire_output = recording_fire
        session = logic.prop_runtime
        session.start()
        return logic, session, events

    yield make

    for logic in reversed(logics):
        try:
            logic.prop_runtime.stop()
        finally:
            logic.stop()


def _floor_grid():
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([{'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}])
    return grid


def test_core_prop_carry_drop_rest_without_plugins(real_logic):
    spin = 90.0  # degrees per second about X
    prop = Prop(pos=[0.0, 40.0, 30.0], properties={
        'carry_enabled': True,
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
    logic, session, events = real_logic([prop], grid=grid, physics=physics)

    # Carry: the prop is directly ahead at eye height.
    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert physics.get_body(prop).kinematic
    assert io.names()[-1] == 'OnCarried'

    # Carry: the prop follows the view; physics must not move it.
    session.tick(1 / 60, use_pressed=False)
    physics.step(1 / 60)
    carried = list(prop.pos)
    assert carried[2] > 30.0
    assert logic.interaction_runtime.current_hud_message == '[E] Carry / Drop'

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


def test_non_physics_prop_still_falls_to_ground_on_drop(real_logic):
    prop = Prop(pos=[0.0, 40.0, 55.0], properties={
        'carry_enabled': True,
        'carry_offset': [0.0, 70.0, 0.0],
    })
    io = IO()
    grid = _floor_grid()
    logic, session, events = real_logic([prop], grid=grid)

    # Pick the prop up, then release it. It has no authored physics at all.
    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    session.tick(1 / 60, use_pressed=True)
    assert session.held is None
    assert id(prop) in session._falling

    for _ in range(60):
        session.tick(1 / 60, use_pressed=False)

    assert abs(prop.pos[1] - 16.0) < 1e-3
    assert id(prop) not in session._falling
    assert 'OnRest' in io.names()


def test_respawn_fades_in_over_two_seconds(real_logic):
    prop = Prop(pos=[0.0, 0.0, 0.0], properties={
        'collect_enabled': True,
        'collect_respawns': True,
    })
    logic, session, events = real_logic([prop])

    assert prop._respawn_fade_alpha == 1.0
    assert session.collect_prop(prop) is True
    assert prop.properties['collect_collected'] is True

    assert session.respawn_prop(prop) is True
    assert prop._respawn_fade_alpha == 0.0

    session._update_respawn_fades(1.0)
    assert prop._respawn_fade_alpha == 0.5

    session._update_respawn_fades(1.0)
    assert prop._respawn_fade_alpha == 1.0
    assert id(prop) not in session.respawn_fades


def test_respawn_fade_state_resets_when_session_restarts(real_logic):
    prop = Prop(pos=[0.0, 0.0, 0.0])
    logic, session, events = real_logic([prop])

    prop._respawn_fade_alpha = 0.0
    session.stop()
    session.start()

    assert prop._respawn_fade_alpha == 1.0


def test_carried_billboard_keeps_its_facing_when_player_turns(real_logic):
    prop = Prop(pos=[0.0, 40.0, 55.0], properties={'carry_enabled': True})
    logic, session, events = real_logic([prop])

    session.tick(1 / 60, use_pressed=True)
    assert session.held is prop
    assert prop._carry_sprite_yaw == 0.0

    logic.player_runtime.player.angle = 1.25
    session.tick(1 / 60, use_pressed=False)
    assert prop._carry_sprite_yaw == 0.0


def test_the_registry_is_derived_from_the_authoritative_thing_list(real_logic):
    """PropSession is the Prop registry; the thing list is still the world."""
    prop = Prop(pos=[0, 0, 0])
    light = Light(pos=[0, 0, 0])
    logic, session, events = real_logic([prop, light])

    assert session.props == [prop]
    assert session.by_id(id(prop)) is prop
    assert session.by_id(id(light)) is None


def test_a_rebuild_adopts_a_new_prop_without_disturbing_the_others(real_logic):
    """A spawn elsewhere in the map must not reset a Prop already registered."""
    settled = Prop(pos=[0, 0, 0])
    logic, session, events = real_logic([settled])

    settled.pos = [10.0, 20.0, 30.0]          # it has moved since it was adopted
    home = list(settled.properties['_prop_home_pos'])

    spawned = Prop(pos=[100, 0, 0])
    logic.editor_state.things.append(spawned)
    session.rebuild()

    assert session.props == [settled, spawned]
    assert settled.properties['_prop_home_pos'] == home, (
        "adopting a new Prop re-homed one that was already registered")
    assert spawned.properties['_prop_home_pos'] == [100.0, 0.0, 0.0]


def test_a_rebuild_releases_a_prop_that_left_the_world(real_logic):
    prop, other = Prop(pos=[0, 0, 0]), Prop(pos=[10, 0, 0])
    logic, session, events = real_logic([prop, other])
    session.held = other

    logic.editor_state.things.remove(other)
    session.rebuild()

    assert session.props == [prop]
    assert session.by_id(id(other)) is None
    assert session.held is None, "the session kept hold of a Prop that is gone"
    assert '_prop_home_pos' not in other.properties, (
        "a released Prop kept the session's authored state")


def test_an_empty_registry_is_a_valid_state(real_logic):
    """A map with no Props still has a session; it just has nothing in it."""
    light = Light(pos=[0, 0, 0])
    logic, session, events = real_logic([light])
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


def test_aabb_collision_shape_skips_mesh_collision(real_logic):
    prop = Prop(pos=[10.0, 20.0, 30.0], properties={
        'model_path': '__missing_model_for_aabb_test__.obj',
        'scale': [2.0, 2.0, 2.0],
        'rotation': [0.0, 0.0, 0.0],
        'collision_shape': 'aabb',
        'collision_size': [0.0, 0.0, 0.0],
        'no_collision': False,
        'physics_enabled': False,
    })
    logic, session, events = real_logic([prop])
    brushes = logic.collision_runtime.build_model_collision_brushes()
    assert len(brushes) == 1
    assert brushes[0]['_collision_mode'] == 'aabb'
    assert brushes[0]['size'] == [128.0, 128.0, 128.0]
    assert brushes[0]['pos'] == [10.0, 20.0, 30.0]
