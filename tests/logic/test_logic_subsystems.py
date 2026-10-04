"""Subsystem-level tests for the LogicThread extraction.

Each test gives one extracted logic_* runtime the smallest host surface needed by
a representative core method. These are intentionally not full LogicThread
integration tests: the point is to catch accidental coupling and
initialization-order regressions at the subsystem boundary.
"""

from types import SimpleNamespace
import threading

import glm
import numpy as np
import pytest

from engine.logic_camera import LogicCamera
from engine.logic_collision import LogicCollision
from engine.logic_combat import LogicCombat
from engine.logic_editor import LogicEditor
from engine.logic_interaction import LogicInteraction
from engine.logic_movers import LogicMovers
from engine.logic_parenting import LogicParenting
from engine.logic_player import LogicPlayer
from engine.logic_portals import LogicPortals
from engine.logic_render import LogicRender
from engine.logic_session import LogicSession
from engine.logic_thread import LogicThread
from engine.logic_timing import LogicTiming
from engine.logic_triggers import LogicTriggers
from engine.logic_world import LogicWorld


class _GameState:
    def __init__(self, keys=(), mouse_delta=(0, 0)):
        self._keys = set(keys)
        self._mouse_delta = mouse_delta
        self.sounds = []

    def consume_mouse_delta(self):
        delta = self._mouse_delta
        self._mouse_delta = (0, 0)
        return delta

    def get_keys(self):
        return self._keys

    def queue_sound(self, sound):
        self.sounds.append(sound)


class _Thing:
    def __init__(self, pos=(0, 0, 0), **properties):
        self.pos = list(pos)
        self.properties = dict(properties)
        self.name = self.properties.get("name", "thing")


class _Portal:
    def __init__(self, name, target):
        self.properties = {"name": name, "portal_target": target}


def test_logic_camera_constructs_and_computes_overhead_footprint():
    player = SimpleNamespace(pos=glm.vec3(0, 0, 0), angle=0.0)
    runtime = LogicCamera(SimpleNamespace(player_runtime=SimpleNamespace(player=player)))

    runtime.set_camera_mode("overhead")
    runtime.overhead_height = 400.0
    footprint = runtime.overhead_ground_footprint()

    assert runtime.is_overhead()
    assert footprint is not None
    assert all(value > 0.0 for value in footprint)


def test_logic_collision_constructs_and_classifies_brushes():
    host = SimpleNamespace(
        editor_state=SimpleNamespace(brushes=[], things=[]),
        session_runtime=SimpleNamespace(
            play_mode=False,
            spatial_grid=None,
            physics_world=None,
        ),
    )
    runtime = LogicCollision(host)

    assert runtime.angled_brush_is_solid({"geometry": {}}) is True
    assert runtime.angled_brush_is_solid({"hidden": True}) is False
    assert runtime.angled_brush_is_solid({"is_trigger": True}) is False
    assert runtime.toggle_model_collision(False) is False
    assert runtime.toggle_model_collision(True) is True


def test_logic_combat_constructs_and_ray_tests_aabb():
    runtime = LogicCombat(SimpleNamespace())

    hit, distance = runtime.intersect_ray_aabb(
        (0, 0, 0),
        (1, 0, 0),
        (5, -1, -1),
        (6, 1, 1),
    )

    assert hit is True
    assert distance == pytest.approx(5.0)


def test_logic_editor_constructs_and_processes_an_idle_tick():
    host = SimpleNamespace(
        game_state=_GameState(),
        EDITOR_CAMERA_SPEED=300.0,
        EDITOR_CAMERA_FAST_MULT=2.5,
        EDITOR_MOUSE_SENSITIVITY=0.15,
    )
    host.camera = LogicCamera(host)
    host.camera.set_editor_camera(glm.vec3(0, 0, 0), 0.0, 0.0, host.camera.editor_camera.fov)
    runtime = LogicEditor(host)

    runtime.tick(1.0 / 60.0)

    assert host.camera.get_editor_camera().pos == glm.vec3(0, 0, 0)


def test_logic_interaction_constructs_and_opens_a_nearby_door():
    opened = []
    door = {
        "pos": [0, 0, 0],
        "size": [32, 64, 32],
        "door_locked": False,
    }
    host = SimpleNamespace(
        player_runtime=SimpleNamespace(
            player=SimpleNamespace(pos=glm.vec3(0, 0, 0))
        ),
        editor_state=SimpleNamespace(brushes=[], things=[]),
        io_manager=None,
        _plugin_emit=lambda *args, **kwargs: opened.append((args, kwargs)),
    )
    host.mover_runtime = LogicMovers(host)
    host.mover_runtime.doors = [(0, door)]
    host.mover_runtime.door_states = {0: {"state": "closed"}}
    host.world_runtime = LogicWorld(host)
    runtime = LogicInteraction(host)

    runtime.handle(True)

    assert host.mover_runtime.door_states[0]["state"] == "opening"
    assert runtime.current_hud_message == "[E] Open"


def test_logic_movers_constructs_and_indexes_mover_brushes():
    mover = {
        "name": "lift",
        "is_mover": True,
        "pos": [0, 0, 0],
    }
    host = SimpleNamespace(
        editor_state=SimpleNamespace(brushes=[mover], things=[]),
        movers=[],
        mover_path_states={},
        _mover_brush_list=[],
    )
    runtime = LogicMovers(host)

    runtime._init_movers()

    assert runtime.movers == [(0, mover)]
    assert runtime.mover_states[0]["progress"] == pytest.approx(0.0)
    assert mover["original_pos"] == [0, 0, 0]


def test_logic_parenting_constructs_and_updates_parented_light():
    class Light:
        def __init__(self):
            self.name = "lamp"
            self.pos = [4, 2, 0]
            self.properties = {"parent_mover": "lift"}

    brush = {"name": "lift", "is_mover": True, "pos": [0, 0, 0]}
    light = Light()
    host = SimpleNamespace(editor_state=SimpleNamespace(brushes=[brush], things=[light]))

    runtime = LogicParenting(host, light_type=Light)
    runtime._init_parented_lights()

    brush["pos"] = [10, 5, 20]
    runtime._update_parented_lights()

    assert light.pos == [14, 7, 20]
    assert len(runtime._parented_lights) == 1


def test_logic_player_constructs_and_reports_water_transition():
    noise = []
    player = SimpleNamespace(
        in_water=True,
        swimming=False,
        on_ground=True,
        velocity=glm.vec3(30, 0, 0),
        pos=glm.vec3(1, 2, 3),
    )
    host = SimpleNamespace(
        game_state=_GameState(),
        _gunfire_events=[],
        _plugin_emit=lambda *args, **kwargs: None,
    )
    host.combat_runtime = LogicCombat(host)
    runtime = LogicPlayer(host)
    runtime.player = player

    runtime.update_water_sounds(0.1)

    assert [sound["file"] for sound in host.game_state.sounds] == [
        "enterwater.wav",
        "waterwalk.wav",
    ]
    assert host.combat_runtime._gunfire_events[0]["source"] == "water_enter"
    assert runtime._player_was_in_water is True


def test_logic_portals_constructs_and_rebuilds_target_links():
    first = _Portal("A", "B")
    second = _Portal("B", "A")
    host = SimpleNamespace(editor_state=SimpleNamespace(brushes=[], things=[first, second]))
    runtime = LogicPortals(host, portal_type=_Portal)

    runtime.rebuild_links()

    assert runtime.portal_slots.tolist() == [0, 1]
    assert runtime.portal_target_slots.tolist() == [1, 0]
    assert runtime.portal_target_things == [second, first]


def test_logic_render_constructs_and_batches_frustum_tests():
    runtime = LogicRender(SimpleNamespace())

    plane = runtime.normalize_plane(2.0, 0.0, 0.0, -2.0)
    visible = runtime.aabb_in_frustum_batch(
        [plane],
        [[2.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]],
    )

    assert plane == pytest.approx((1.0, 0.0, 0.0, -1.0))
    assert np.array_equal(visible, np.array([True, False]))


def test_logic_session_constructs_and_releases_session_cache_state():
    reset = []
    from engine.logic_movers import LogicMovers

    player = SimpleNamespace(ground_object=object())
    host = SimpleNamespace(
        editor_state=SimpleNamespace(brushes=[], things=[]),
        io_manager=SimpleNamespace(reset=lambda: reset.append(True)),
        player_runtime=SimpleNamespace(player=player, player2=None),
    )
    host._monster_lock = threading.RLock()
    host.monster_ai = SimpleNamespace(monster_states={})
    host.trigger_runtime = LogicTriggers(host)
    host.portal_runtime = LogicPortals(host)
    host.world_runtime = LogicWorld(host)
    host.collision_runtime = LogicCollision(host)
    host.mover_runtime = LogicMovers(host)
    host.collision_runtime._collision_brushes_cache = [1]
    host.collision_runtime._model_collision_brushes = [2]
    host.collision_runtime._physics_body_brushes = [3]
    host.mover_runtime._mover_brush_list = [4]
    host.mover_runtime._door_brush_list = [5]
    host.world_runtime.monster_spawn_health = {"monster": 100}
    runtime = LogicSession(host)

    runtime.release_session_caches()

    assert reset == [True]
    assert player.ground_object is None
    assert host.collision_runtime._collision_brushes_cache == []
    assert host.world_runtime.monster_spawn_health == {}


def test_logic_timing_constructs_and_updates_light_fade():
    light = _Thing()
    light.properties.update({"intensity": 0.0})
    host = SimpleNamespace(io_manager=None, world_runtime=SimpleNamespace(timer_things=[]))
    runtime = LogicTiming(host)
    runtime.light_fade_states["lamp"] = {
        "entity": light,
        "elapsed": 0.0,
        "duration": 1.0,
        "from": 0.0,
        "to": 1.0,
        "end_off": True,
    }

    runtime.update_light_fades(1.0)

    assert light.properties["intensity"] == pytest.approx(1.0)
    assert light.properties["state"] == "off"
    assert runtime.light_fade_states == {}


def test_logic_triggers_constructs_and_owns_hurt_cadence():
    runtime = LogicTriggers(SimpleNamespace())

    assert runtime.HURT_INTERVAL == pytest.approx(0.5)
    assert not hasattr(runtime.logic, "HURT_INTERVAL")
    assert runtime._trigger_filters({"trigger_filters": "Player"}) == {"player"}

    inside = runtime.use_trigger_contains(
        np.array([3.0, 9.0]),
        np.array([4.0, 2.0]),
    )
    assert np.array_equal(inside, np.array([True, False]))


def test_logic_world_constructs_and_packs_levelchanger_rows():
    first = _Thing((1, 2, 3), name="first", radius=32)
    second = _Thing((4, 5, 6), name="second", radius=64, disabled=True)
    host = SimpleNamespace(
        editor_state=SimpleNamespace(brushes=[], things=[first, second]),
    )
    host.trigger_runtime = LogicTriggers(host)
    host._monster_lock = threading.RLock()
    host.monster_ai = SimpleNamespace(monster_states={})
    host.portal_runtime = LogicPortals(host)
    from engine.prop_runtime import PropSession
    host.session_runtime = SimpleNamespace(physics_world=None, play_mode=False)
    host.prop_runtime = PropSession(host)
    runtime = LogicWorld(host, levelchanger_type=_Thing)

    runtime.refresh_levelchanger_table()
    runtime.build_entity_caches()
    assert runtime.name_cache["first"] is first

    assert runtime.levelchanger_centres.dtype == np.float32
    assert np.array_equal(
        runtime.levelchanger_centres,
        np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32),
    )
    assert np.array_equal(
        runtime.levelchanger_radii,
        np.array([32, 64], dtype=np.float32),
    )
    assert np.array_equal(runtime.levelchanger_eligible, np.array([True, False]))


def _contract_host():
    host = object.__new__(LogicThread)
    host.editor_state = SimpleNamespace(brushes=[], things=[])

    # The mover state properties are descriptors: their setters reach the mover
    # table and therefore require the backing lists to exist first.
    # mover_states/door_states are properties over the dense MoverTable;
    # seed the backing table rather than assigning the old dict interface.
    from engine.mover_table import MoverTable
    host._mover_table = MoverTable()

    host.movers = []
    host.doors = []

    special = {"movers", "doors", "mover_states", "door_states"}
    for attribute in {
        attr
        for attrs in LogicThread._RUNTIME_HOST_CONTRACTS.values()
        for attr in attrs
    } - special:
        if attribute not in {"brushes", "things"}:
            setattr(host, attribute, None)

    host.player_runtime = SimpleNamespace(player=SimpleNamespace())
    host.camera = SimpleNamespace(
        player_runtime=SimpleNamespace(player=host.player_runtime.player)
    )
    # The mover state properties delegate through mover_runtime. Use the real
    # subsystem here so hasattr(host, "mover_states") exercises that contract
    # rather than a generic stub with no state properties.
    host.mover_runtime = LogicMovers(host)
    for runtime_name in LogicThread._RUNTIME_HOSTS:
        if runtime_name in ("camera", "mover_runtime"):
            continue
        setattr(host, runtime_name, SimpleNamespace(logic=host))
    return host


def test_logic_thread_runtime_contract_validation_catches_missing_host_state():
    host = _contract_host()

    host._validate_runtime_contracts()

    del host.player_runtime

    with pytest.raises(AssertionError, match="player_runtime"):
        host._validate_runtime_contracts()
