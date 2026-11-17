"""Plugin API 2.0: 1.9, extended to expose Soma and the player.

Nothing in 1.9 is changed or removed. Soma is Fio's physics engine -- a
subsystem of the engine, as the Forward renderer is, not a plugin -- and what
2.0 adds is how a plugin communicates with it and with the player, and the
means to swap the physics engine out as the renderer can be.
"""
import json
import pathlib
import re

import pytest

import plugins.api as api
from plugins.api import (API_VERSION, API_VERSION_INFO, Body, Physics,
                         PlayerState, RuntimeAPI, version_tuple)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_the_api_is_2_0():
    assert API_VERSION == "2.0.0"
    assert API_VERSION_INFO == version_tuple(API_VERSION) == (2, 0, 0)


def test_the_physics_contract_is_importable_from_the_api():
    import engine.physics as boundary

    for name in ('Physics', 'Body', 'HEAVY_MASS', 'CRUSH_DAMAGE', 'SV_GRAVITY',
                 'world_gravity'):
        assert getattr(api, name) is getattr(boundary, name)
    assert api.world_gravity() == -api.SV_GRAVITY


@pytest.mark.parametrize('plugin', ['bigworld', 'deathmatch', 'tidy'])
def test_the_shipped_plugins_are_built_against_2_0(plugin):
    source = (ROOT / 'plugins' / plugin / 'plugin.py').read_text()
    assert re.search(r'^\s+api_version = "2\.0\.0"', source, re.M)


def test_nothing_in_api_1_9_is_missing_from_2_0():
    """The whole public surface of 1.9 -- every module-level name, every
    public member of every class -- recorded from the 1.9 source, is still
    here. 2.0 only adds."""
    surface = json.loads(
        (ROOT / 'tests/plugins/fixtures/api_1_9_surface.json').read_text())
    missing = [name for name in surface['module'] if not hasattr(api, name)]
    for cls, members in surface['classes'].items():
        current = getattr(api, cls, None)
        declared = getattr(current, '__annotations__', {})
        missing += [f'{cls}.{member}' for member in members
                    if current is None
                    or not (hasattr(current, member) or member in declared)]
    assert not missing, missing


def test_soma_is_a_subsystem_of_the_engine_not_a_plugin():
    """Soma lives in the engine, and no plugin package holds a physics."""
    from engine.physics import DEFAULT_PHYSICS, physics_factory

    soma = physics_factory(DEFAULT_PHYSICS)
    assert DEFAULT_PHYSICS == "Soma"
    assert soma.__module__ == 'engine.physics.soma'
    assert (ROOT / 'engine' / 'physics' / 'soma' / 'full.py').is_file()
    assert not list((ROOT / 'plugins').rglob('*physics*.py'))


def test_the_physics_engine_can_be_swapped_out_as_the_renderer_can():
    """``register_physics`` beside ``register_renderer``: another engine is
    registered through the API and selected for the next play session."""
    from engine.physics import create_physics, registry, selected_physics
    from engine.spatial_grid import SpatialGrid
    from plugins.api import EditorAPI
    from tests.helpers.physics import StubPhysics

    saved = dict(registry._factories), registry._selected
    editor_api = EditorAPI(None, None)
    try:
        assert editor_api.register_physics("Stub", StubPhysics) is True
        assert editor_api.select_physics("NoSuchPhysics") is False
        assert selected_physics() == "Soma"
        assert editor_api.select_physics("Stub") is True
        assert isinstance(create_physics(SpatialGrid()), StubPhysics)
        assert editor_api.select_physics("Soma") is True
        assert type(create_physics(SpatialGrid())).__module__.startswith(
            'engine.physics.soma')
    finally:
        registry._factories.clear()
        registry._factories.update(saved[0])
        registry._selected = saved[1]


# -- a play session -----------------------------------------------------------

@pytest.fixture
def session():
    pytest.importorskip("PyQt5", reason="a play session needs the real LogicThread")
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.prop_entity import Prop
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = [{'pos': [0.0, -50.0, 0.0], 'size': [4000.0, 100.0, 4000.0]}]
    prop = Prop(pos=[200.0, 0.0, 0.0], properties={
        'physics_enabled': True, 'no_collision': False,
        'collision_size': [40.0, 40.0, 40.0]})
    state.things = [prop]
    logic = LogicThread(ThreadedGameState(), state)
    runtime = RuntimeAPI(None, logic, None)
    assert runtime.physics is None                      # not playing yet
    assert runtime.spatial_grid is None
    assert runtime.body(prop) is None
    logic.session_runtime.apply_play_mode(True)
    if logic.player_runtime.player is None:
        logic.player_runtime.player = Player(0.0, 0.0)
    try:
        yield runtime, logic, prop
    finally:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


@pytest.mark.qt
def test_a_plugin_communicates_with_soma_through_the_runtime_api(session):
    runtime, logic, _prop = session

    physics = runtime.physics
    assert physics is logic.session_runtime.physics_world
    assert isinstance(physics, Physics)
    # It is Soma, so it is also reachable as Soma, with what is Soma's own.
    assert runtime.soma is physics
    assert runtime.soma.tier in ('full', 'crude')
    assert runtime.spatial_grid is logic.session_runtime.spatial_grid

    physics.gravity = -100.0
    assert api.world_gravity(runtime.physics) == -100.0
    physics.reset_controls()
    assert physics.gravity == -api.SV_GRAVITY


@pytest.mark.qt
def test_the_runtime_api_reaches_a_body(session):
    runtime, _logic, prop = session
    if runtime.body(prop) is None:
        # The session derives bodies from model bounds; this Prop has no
        # model, so hand the physics its body brush as the engine would.
        runtime.physics.rebuild([{
            'pos': [200.0, 20.0, 0.0], 'size': [40.0, 40.0, 40.0],
            '_physics_body': True, '_physics_entity': prop}])
    body = runtime.body(prop)
    assert isinstance(body, Body)
    assert body.entity is prop

    runtime.physics.wake(prop, [0.0, 0.0, 90.0])
    assert body.awake is True
    assert list(body.velocity) == [0.0, 0.0, 90.0]


@pytest.mark.qt
def test_the_player_is_read_and_written_through_the_api(session):
    runtime, logic, _prop = session
    player = runtime.player
    assert isinstance(player, PlayerState)
    assert player.exists and player.object is logic.player_runtime.player

    player.teleport((10.0, 60.0, -20.0), velocity=(1.0, 2.0, 3.0))
    assert player.position == (10.0, 60.0, -20.0)
    assert player.velocity == (1.0, 2.0, 3.0)
    # Written as the host's own vector type, read back as plain tuples.
    assert type(player.object.pos) is type(player.object.velocity)
    assert not isinstance(player.position, type(player.object.pos))
    assert player.on_ground is False

    player.yaw, player.pitch = 0.0, 0.0
    assert player.forward == pytest.approx((0.0, 0.0, 1.0))
    assert player.eye == pytest.approx(
        (10.0, 60.0 + player.object.camera_height, -20.0))
    assert player.size == (player.object.width, player.object.height,
                           player.object.depth)


@pytest.mark.qt
def test_the_players_health_and_armour_go_through_the_engines_rules(session):
    runtime, logic, _prop = session
    player = runtime.player

    player.health = 10_000
    assert player.health == player.max_health          # clamped
    player.armor = 30
    player.damage(50)
    assert player.armor == 0                           # armour first
    assert player.health == player.max_health - 20

    player.god_mode = True
    player.damage(1000)
    assert player.health == player.max_health - 20     # god mode stops it
    player.god_mode = False
    assert player.dead is False
    assert logic.player_runtime.player_health == player.health
