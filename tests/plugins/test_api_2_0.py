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
    source = (ROOT / 'plugins' / plugin / 'plugin.py').read_text(encoding='utf-8')
    assert re.search(r'^\s+api_version = "2\.0\.0"', source, re.M)


def test_nothing_in_api_1_9_is_missing_from_2_0():
    """The whole public surface of 1.9 -- every module-level name, every
    public member of every class -- recorded from the 1.9 source, is still
    here. 2.0 only adds."""
    surface = json.loads(
        (ROOT / 'tests/plugins/fixtures/api_1_9_surface.json').read_text(encoding='utf-8'))
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
    assert (ROOT / 'engine' / 'physics' / 'soma.py').is_file()
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
    assert not runtime.physics and not runtime.physics_enabled   # not playing yet
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
    assert physics and runtime.physics_enabled
    # It is Soma, so it is also reachable as Soma.
    assert runtime.soma is physics
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


# -- what a plugin does with Soma ---------------------------------------------

def _give_body(runtime, prop):
    if runtime.body(prop) is None:
        runtime.physics.rebuild([{
            'pos': [prop.pos[0], prop.pos[1] + 20.0, prop.pos[2]],
            'size': [40.0, 40.0, 40.0],
            '_physics_body': True, '_physics_entity': prop}])
    return runtime.body(prop)


@pytest.mark.qt
def test_a_plugin_pushes_and_finds_bodies_through_the_runtime_api(session):
    runtime, _logic, prop = session
    body = _give_body(runtime, prop)

    assert runtime.impulse(prop, [80.0, 0.0, 0.0]) is True
    assert body.velocity[0] == pytest.approx(80.0 / body.mass)
    assert runtime.bodies_near(prop.pos, 100.0) == [prop]
    assert runtime.bodies_in_box([prop.pos[0] - 50, -500, prop.pos[2] - 50],
                                 [prop.pos[0] + 50, 500, prop.pos[2] + 50]) == [prop]
    entity, distance = runtime.raycast_bodies(
        [prop.pos[0] - 300.0, prop.pos[1] + 20.0, prop.pos[2]], [1.0, 0.0, 0.0])
    assert entity is prop and distance == pytest.approx(280.0)
    pushed = runtime.explode([prop.pos[0] - 50.0, prop.pos[1] + 20.0, prop.pos[2]], 200.0, 500.0)
    assert pushed == [prop] and body.velocity[0] > 80.0


def test_with_physics_off_nothing_a_plugin_asks_fails():
    """Physics switched off, or no play session: there is no engine, and a
    plugin that talks to one anyway is answered quietly, never with an
    error."""
    from plugins.api import NO_PHYSICS

    class NoSession:
        io_manager = None
        session_runtime = type('S', (), {'physics_world': None, 'spatial_grid': None})()
    runtime = RuntimeAPI(None, NoSession(), None)
    thing = object()

    assert runtime.physics_enabled is False
    physics = runtime.physics
    assert physics is NO_PHYSICS and not physics            # never None; falsy
    assert isinstance(physics, Physics)                     # the whole contract
    assert runtime.soma is NO_PHYSICS

    # The API's own calls.
    assert runtime.body(thing) is None
    assert runtime.impulse(thing, [1.0, 0.0, 0.0]) is False
    assert runtime.explode([0, 0, 0], 100.0, 100.0) == []
    assert runtime.bodies_near([0, 0, 0], 100.0) == []
    assert runtime.bodies_in_box([0, 0, 0], [1, 1, 1]) == []
    assert runtime.raycast_bodies([0, 0, 0], [1, 0, 0]) is None

    # The engine itself, as a plugin would use it without asking first.
    physics.wake(thing, [0.0, 100.0, 0.0])
    physics.set_kinematic(thing, True)
    physics.sync_entity_position(thing, wake=True)
    physics.on_rest(thing, print)
    physics.off_rest(thing, print)
    physics.wake_all()
    physics.reset_controls()
    physics.step(1.0 / 60.0)
    assert physics.get_body(thing) is None
    assert physics.impulse(thing, [1.0, 0.0, 0.0]) is False
    assert physics.impulse_radial([0, 0, 0], 10.0, 10.0) == []
    assert physics.bodies_near([0, 0, 0], 10.0) == []
    assert physics.raycast([0, 0, 0], [1, 0, 0], 10.0) is None
    assert physics.drain_events() == [] and physics.remove_body(thing) is False
    # Controls are accepted and forgotten; reads are the defaults.
    physics.gravity = -5.0
    physics.time_scale = 0.0
    assert physics.gravity == -api.SV_GRAVITY and physics.time_scale == 1.0
    assert api.world_gravity(physics) == -api.SV_GRAVITY
    # What is one engine's own does nothing too.
    assert runtime.soma.topple(thing, [1.0, 0.0, 0.0]) is None
    assert runtime.soma.stand(thing) is None


@pytest.mark.qt
def test_physics_events_reach_a_subscribed_plugin(session):
    from engine.physics import PHYSICS_EVENTS

    runtime, logic, prop = session
    _give_body(runtime, prop)
    heard = []

    class Plugins:
        """The manager's two members the engine uses for events."""
        def has_listeners(self, event):
            return event == 'physics.rest'

        def emit(self, event, **data):
            heard.append((event, data.get('entity')))

        def __getattr__(self, name):                   # the rest: nothing to do
            return lambda *args, **kwargs: None

    assert set(PHYSICS_EVENTS) == {'physics.rest', 'physics.landed',
                                   'physics.contact', 'physics.toppled'}
    plugins = logic.plugins
    logic.plugins = Plugins()
    prop.pos = [200.0, 300.0, 0.0]
    runtime.physics.sync_entity_position(prop, wake=True)
    for _ in range(400):
        logic._tick_play_mode(1.0 / 60.0)
        if any(event == 'physics.rest' for event, _entity in heard):
            break
    logic.plugins = plugins
    assert ('physics.rest', prop) in heard
    assert ('physics.landed', prop) in heard          # wanted is all or nothing


@pytest.mark.qt
def test_a_physics_prop_spawned_in_play_gets_a_body_and_despawned_loses_it(session):
    from engine.prop_entity import Prop

    runtime, logic, prop = session
    runtime._manager = type('M', (), {'emit': lambda self, *a, **k: None,
                                      '_log': lambda self, m: None})()
    runtime._plugin = type('P', (), {'name': 'test'})()
    spawned = runtime.spawn(Prop, pos=[0.0, 300.0, 300.0], properties={
        'physics_enabled': True, 'no_collision': False, 'collision_shape': 'aabb',
        'model_path': 'assets/models/Oil_Drum.obj'})
    assert spawned is not None
    body = runtime.body(spawned)
    assert body is not None                             # it is simulated
    runtime.physics.wake(spawned)
    for _ in range(5):
        runtime.physics.step(1.0 / 60.0)
    assert spawned.pos[1] < 300.0

    assert runtime.despawn(spawned) is True
    assert runtime.body(spawned) is None


# -- LogicState ----------------------------------------------------------------

@pytest.mark.qt          # LogicState is an editor entity (editor.things needs Qt)
def test_logicstate_values_are_typed_and_writes_are_events():
    from editor.things import LogicState

    heard = []

    class Logic:
        io_manager = None
        session_runtime = type('S', (), {'physics_world': None, 'spatial_grid': None})()
        editor_state = type('E', (), {'things': []})()

        def _plugin_emit(self, event, **data):
            heard.append((event, data['store'], data['key'], data['value']))

    saved = dict(LogicState._persistent_registry)
    try:
        runtime = RuntimeAPI(None, Logic(), None)
        store = runtime.state('api2_test')
        assert store.set('kills', 5) == (True, True)
        assert store.set('kills', 5) == (True, False)          # nothing changed
        assert store.get('kills') == 5 and isinstance(store.get('kills'), int)
        assert store.set('open', True) == (True, True)
        assert store.get('open') is True
        assert 'kills' in store and sorted(store.keys()) == ['kills', 'open']
        assert 'api2_test' in runtime.state_stores()
        assert heard == [('state_changed', 'api2_test', 'kills', 5),
                         ('state_changed', 'api2_test', 'open', True)]
        # The string-speaking store of 1.x sees the same values.
        assert api.GlobalStore().get('kills', store='api2_test') == '5'
        assert store.delete('kills') is True and store.get('kills', 'gone') == 'gone'
        assert store.set('  ', 1) == (False, False)
    finally:
        LogicState._persistent_registry.clear()
        LogicState._persistent_registry.update(saved)


@pytest.mark.qt
def test_a_write_through_the_maps_logicstate_fires_its_outputs(session):
    from editor.things import LogicState

    runtime, logic, _prop = session
    saved = dict(LogicState._persistent_registry)
    io_manager = logic.io_manager
    fired = []
    try:
        entity = LogicState(pos=[0, 0, 0], properties={'store_name': 'doors'})
        logic.editor_state.things.append(entity)
        logic.io_manager = type('IO', (), {
            'fire_output': lambda self, e, name, value=None: fired.append((name, value))})()
        store = runtime.state('doors')
        assert store.set('code', '007', 'string') == (True, True)
        assert store.get('code') == '007'
        assert store.set('count', '3') == (True, True)         # typed as I/O would
        assert store.get('count') == 3
        assert ('OnValueChanged', 'count=3') in fired
        assert ('OnValueSet', 'code=007') in fired
    finally:
        logic.io_manager = io_manager
        LogicState._persistent_registry.clear()
        LogicState._persistent_registry.update(saved)


# -- more of the player --------------------------------------------------------

@pytest.mark.qt
def test_more_of_the_player_is_reachable(session):
    runtime, logic, _prop = session
    player = runtime.player
    assert runtime.player is player                     # one view, made once
    assert player.index == 0

    player.speed = 123.0
    assert player.object.speed == 123.0
    assert player.noclip is False
    player.noclip = True
    assert player.object.physics_enabled is False
    player.noclip = False

    assert player.keys == frozenset()
    player.give_key('red')
    assert player.has_key('red') and player.keys == frozenset({'red'})
    assert player.take_key('red') is True and player.take_key('red') is False
    assert isinstance(player.weapons, frozenset)
    player.notarget = True
    assert logic.player_runtime.notarget is True
    assert player.holding is None and player.drop() is False

    second = runtime.player2
    assert second.index == 1 and second.exists is False  # nobody on split-screen
    assert second.position == (0.0, 0.0, 0.0)


def test_flags_are_set_through_the_api():
    from engine.prop_entity import Prop

    prop = Prop(properties={})
    assert api.set_flag(prop, 'disabled', True) is True
    assert prop.properties['disabled'] is True
    assert api.clear_flag(prop, 'disabled') is True
    assert 'disabled' not in prop.properties
    assert api.set_flag(object(), 'disabled', True) is False


# -- locked --------------------------------------------------------------------

def _surface():
    """The public surface of ``plugins/api.py``, read from its source: every
    module-level name it defines or re-exports from the engine, and every
    public member of every public class."""
    import ast

    tree = ast.parse((ROOT / 'plugins' / 'api.py').read_text(encoding='utf-8'))
    module, classes = set(), {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith('_'):
            module.add(node.name)
            if isinstance(node, ast.ClassDef):
                members = set()
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and not item.name.startswith('_'):
                        members.add(item.name)
                    elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                        members.update(t.id for t in targets
                                       if isinstance(t, ast.Name) and not t.id.startswith('_'))
                classes[node.name] = sorted(members)
        elif isinstance(node, ast.Assign):
            module.update(t.id for t in node.targets
                          if isinstance(t, ast.Name) and not t.id.startswith('_'))
        elif isinstance(node, ast.ImportFrom) and (node.module or '').startswith('engine'):
            module.update(a.asname or a.name for a in node.names
                          if not (a.asname or a.name).startswith('_'))
    return {'module': sorted(module), 'classes': classes}


def test_the_api_surface_is_locked_at_2_0():
    """API 2.0 is this surface, exactly. A name that disappears breaks
    plugins; a name that appears is a new API and needs a new version:
    either way this fails until ``API_VERSION`` moves and the recorded
    surface (``fixtures/api_2_0_surface.json``) moves with it."""
    locked = json.loads(
        (ROOT / 'tests/plugins/fixtures/api_2_0_surface.json').read_text(encoding='utf-8'))
    now = _surface()
    removed = sorted(set(locked['module']) - set(now['module']))
    added = sorted(set(now['module']) - set(locked['module']))
    for cls in sorted(set(locked['classes']) | set(now['classes'])):
        was, is_ = set(locked['classes'].get(cls, ())), set(now['classes'].get(cls, ()))
        removed += [f'{cls}.{m}' for m in sorted(was - is_)]
        added += [f'{cls}.{m}' for m in sorted(is_ - was)]
    assert not removed, f"removed from API {API_VERSION}: {removed}"
    assert not added, (f"added to API {API_VERSION} without a new version: {added}")
    assert API_VERSION == "2.0.0"


def test_a_store_finds_its_entity_once_and_again_when_the_level_changes():
    """A read must not walk every thing in the level -- and must still see a
    store entity that arrives, leaves or is replaced."""
    class Store:
        def __init__(self, name):
            self.store_name, self._runtime_data = name, {}

        def apply_value(self, key, value, value_type=None):
            changed = self._runtime_data.get(key) != value
            self._runtime_data[key] = value
            return True, changed, value

    class Things(list):
        walks = 0

        def __iter__(self):
            Things.walks += 1
            return super().__iter__()

    things = Things(object() for _ in range(50))
    logic = type('Logic', (), {
        'io_manager': None, 'editor_state': type('E', (), {'things': things})(),
        'session_runtime': type('S', (), {'physics_world': None, 'spatial_grid': None})()})()
    registry = api.StateStore._registry()
    saved = dict(registry)
    try:
        store = RuntimeAPI(None, logic, None).state('cached_store')
        assert store.get('k') is None
        walks = Things.walks
        for _ in range(10):
            store.get('k')
        assert Things.walks == walks                    # no entity: remembered

        first = Store('cached_store')
        first._runtime_data['k'] = 1
        things.append(first)                            # one arrives
        assert store.get('k') == 1
        walks = Things.walks
        for _ in range(10):
            assert store.get('k') == 1
        assert Things.walks == walks                    # found: remembered

        second = Store('cached_store')
        second._runtime_data['k'] = 2
        things[things.index(first)] = second            # replaced in place
        assert store.get('k') == 2
        things.insert(0, object())                      # moved along
        assert store.get('k') == 2
        second.store_name = 'renamed'                   # no longer this store
        assert store.get('k') is None
        second.store_name = 'cached_store'
        things.remove(second)                           # gone
        assert store.get('k') is None
        logic.editor_state.things = Things([Store('cached_store')])   # a new level
        logic.editor_state.things[0]._runtime_data['k'] = 3
        assert store.get('k') == 3
    finally:
        registry.clear()
        registry.update(saved)


def test_the_physics_package_exports_the_boundary_only():
    """No compatibility names: the world and Soma's classes are imported from
    their own modules (engine.spatial_grid, engine.physics.soma)."""
    import engine.physics as boundary
    from engine.physics import soma
    from engine import spatial_grid

    assert not hasattr(boundary, '__getattr__')
    for name in ('SpatialGrid', 'Soma', 'PhysicsWorld', 'PhysicsBody',
                 'quat_from_rotation', 'rotation_from_quat'):
        assert name not in vars(boundary), name
        with pytest.raises(ImportError):
            exec(f'from engine.physics import {name}', {})
    assert spatial_grid.SpatialGrid and soma.Soma and soma.PhysicsBody
    assert not hasattr(soma, 'PhysicsWorld'), "Soma has one name"

