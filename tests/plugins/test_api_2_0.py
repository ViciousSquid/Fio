"""Plugin API 2.0: physics and the player are part of the API.

Fio's core is modular -- the physics is an engine module behind a contract,
not a plugin -- and 2.0 is the surface anything outside the engine reaches it
and the player through: ``RuntimeAPI.physics``, ``RuntimeAPI.body``,
``RuntimeAPI.spatial_grid`` and ``RuntimeAPI.player``.
"""
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
                 'world_gravity', 'available_physics', 'selected_physics'):
        assert getattr(api, name) is getattr(boundary, name)
    assert api.world_gravity() == -api.SV_GRAVITY


@pytest.mark.parametrize('plugin', ['bigworld', 'deathmatch', 'tidy'])
def test_the_shipped_plugins_are_built_against_2_0(plugin):
    source = (ROOT / 'plugins' / plugin / 'plugin.py').read_text()
    assert re.search(r'^\s+api_version = "2\.0\.0"', source, re.M)


def test_physics_is_not_a_plugin():
    """Soma is engine core: it lives in the engine, and no plugin package
    holds a physics the engine depends on."""
    from engine.physics import DEFAULT_PHYSICS, physics_factory

    soma = physics_factory(DEFAULT_PHYSICS)
    assert soma.__module__ == 'engine.physics.soma'
    assert (ROOT / 'engine' / 'physics' / 'soma.py').is_file()


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
def test_the_runtime_api_reaches_the_live_physics(session):
    runtime, logic, _prop = session

    physics = runtime.physics
    assert physics is logic.session_runtime.physics_world
    assert isinstance(physics, Physics)
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
