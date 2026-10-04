"""Subsystem-level tests for the LogicThread extraction.

Each test gives one extracted logic_* runtime the smallest host surface needed by
a representative core method. These are intentionally not full LogicThread
integration tests: the point is to catch accidental coupling and
initialization-order regressions at the subsystem boundary.
"""

from types import SimpleNamespace

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
    runtime = LogicCamera(player=player)

    runtime.set_camera_mode("overhead")
    runtime.overhead_height = 400.0
    footprint = runtime.overhead_ground_footprint()

    assert runtime.is_overhead()
    assert footprint is not None
    assert all(value > 0.0 for value in footprint)


def test_logic_collision_constructs_and_classifies_brushes():
    runtime = LogicCollision(SimpleNamespace())

    assert runtime.angled_brush_is_solid({"geometry": {}}) is True
    assert runtime.angled_brush_is_solid({"hidden": True}) is False
    assert runtime.angled_brush_is_solid({"is_trigger": True}) is False


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
        editor_camera=SimpleNamespace(
            yaw=0.0,
            pitch=0.0,
            pos=glm.vec3(0, 0, 0),
        ),
        EDITOR_CAMERA_SPEED=300.0,
        EDITOR_CAMERA_FAST_MULT=2.5,
        EDITOR_MOUSE_SENSITIVITY=0.15,
        _editor_mouselook_active=False,
    )
    runtime = LogicEditor(host)

    runtime.tick(1.0 / 60.0)

    assert host.editor_camera.pos == glm.vec3(0, 0, 0)
    assert host._editor_mouselook_active is False


def test_logic_interaction_constructs_and_opens_a_nearby_door():
    opened = []
    door = {
        "pos": [0, 0, 0],
        "size": [32, 64, 32],
        "door_locked": False,
    }
    host = SimpleNamespace(
        player=SimpleNamespace(pos=glm.vec3(0, 0, 0)),
        doors=[(0, door)],
        door_states={0: {"state": "closed"}},
        collected_keys=set(),
        io_manager=None,
        current_hud_message="stale",
        current_hud_key_name="stale",
        _levelchanger_things=[],
        level_complete_ui=None,
        _trigger_door_open=lambda idx, brush: opened.append((idx, brush)),
    )
    runtime = LogicInteraction(host)

    runtime.handle(True)

    assert opened == [(0, door)]
    assert host.current_hud_message == "[E] Open"


def test_logic_movers_constructs_and_indexes_mover_brushes():
    mover = {
        "name": "lift",
        "is_mover": True,
        "pos": [0, 0, 0],
    }
    host = SimpleNamespace(
        brushes=[mover],
        movers=[],
        mover_states={},
        mover_path_states={},
        _mover_brush_list=[],
    )
    runtime = LogicMovers(host)

    runtime._init_movers()

    assert host.movers == [(0, mover)]
    assert host.mover_states[0]["progress"] == pytest.approx(0.0)
    assert mover["original_pos"] == [0, 0, 0]


def test_logic_parenting_constructs_and_updates_parented_light():
    class Light:
        def __init__(self):
            self.name = "lamp"
            self.pos = [4, 2, 0]
            self.properties = {"parent_mover": "lift"}

    brush = {"name": "lift", "is_mover": True, "pos": [0, 0, 0]}
    light = Light()
    host = SimpleNamespace(brushes=[brush], things=[light], _parented_lights=[])

    runtime = LogicParenting(host, light_type=Light)
    runtime._init_parented_lights()

    brush["pos"] = [10, 5, 20]
    runtime._update_parented_lights()

    assert light.pos == [14, 7, 20]
    assert len(host._parented_lights) == 1


def test_logic_player_constructs_and_reports_water_transition():
    noise = []
    host = SimpleNamespace(
        player=SimpleNamespace(
            in_water=True,
            swimming=False,
            on_ground=True,
            velocity=glm.vec3(30, 0, 0),
            pos=glm.vec3(1, 2, 3),
        ),
        player2=None,
        game_state=_GameState(),
        _player_was_in_water=False,
        _waterwalk_timer=0.0,
        WATERWALK_INTERVAL=0.45,
        _emit_noise_event=lambda pos, source, loudness: noise.append(
            (tuple(pos), source, loudness)
        ),
    )
    runtime = LogicPlayer(host)

    runtime.update_water_sounds(0.1)

    assert [sound["file"] for sound in host.game_state.sounds] == [
        "enterwater.wav",
        "waterwalk.wav",
    ]
    assert noise[0][1] == "water_enter"
    assert host._player_was_in_water is True


def test_logic_portals_constructs_and_rebuilds_target_links():
    first = _Portal("A", "B")
    second = _Portal("B", "A")
    host = SimpleNamespace(things=[first, second])
    runtime = LogicPortals(host, portal_type=_Portal)

    runtime.rebuild_links()

    assert host._portal_slots.tolist() == [0, 1]
    assert host._portal_target_slots.tolist() == [1, 0]
    assert host._portal_target_things == [second, first]


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
    released = []
    reset = []
    world = SimpleNamespace(release_session_indexes=lambda: released.append(True))
    io_manager = SimpleNamespace(reset=lambda: reset.append(True))
    player = SimpleNamespace(ground_object=object())

    host = SimpleNamespace(
        _world_runtime=lambda: world,
        _collision_brushes_cache=[1],
        _model_collision_brushes=[2],
        _physics_body_brushes=[3],
        _mover_brush_list=[4],
        _door_brush_list=[5],
        _monster_spawn_health={"monster": 100},
        io_manager=io_manager,
        player=player,
        player2=None,
    )
    runtime = LogicSession(host)

    runtime.release_session_caches()

    assert released == [True]
    assert reset == [True]
    assert player.ground_object is None
    assert host._collision_brushes_cache == []
    assert host._monster_spawn_health == {}


def test_logic_timing_constructs_and_updates_light_fade():
    light = _Thing()
    light.properties.update({"intensity": 0.0})
    host = SimpleNamespace(
        light_fade_states={
            "lamp": {
                "entity": light,
                "elapsed": 0.0,
                "duration": 1.0,
                "from": 0.0,
                "to": 1.0,
                "end_off": True,
            }
        },
        io_manager=None,
    )
    runtime = LogicTiming(host)

    runtime.update_light_fades(1.0)

    assert light.properties["intensity"] == pytest.approx(1.0)
    assert light.properties["state"] == "off"
    assert host.light_fade_states == {}


def test_logic_triggers_constructs_and_uses_authored_sphere_radius():
    runtime = LogicTriggers(SimpleNamespace())

    assert runtime._trigger_filters({"trigger_filters": "Player"}) == {"player"}

    inside = runtime.use_trigger_contains(
        np.array([3.0, 9.0]),
        np.array([4.0, 2.0]),
    )
    assert np.array_equal(inside, np.array([True, False]))


def test_logic_world_constructs_and_packs_levelchanger_rows():
    first = _Thing((1, 2, 3), name="first", radius=32)
    second = _Thing((4, 5, 6), name="second", radius=64, disabled=True)
    host = SimpleNamespace(_levelchanger_things=[first, second])
    runtime = LogicWorld(host)

    runtime.refresh_levelchanger_table()

    assert host._levelchanger_centres.dtype == np.float32
    assert np.array_equal(
        host._levelchanger_centres,
        np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32),
    )
    assert np.array_equal(
        host._levelchanger_radii,
        np.array([32, 64], dtype=np.float32),
    )
    assert np.array_equal(host._levelchanger_eligible, np.array([True, False]))


def _contract_host():
    host = object.__new__(LogicThread)
    host.editor_state = SimpleNamespace(brushes=[], things=[])
    for runtime_name in LogicThread._RUNTIME_HOSTS:
        setattr(host, runtime_name, SimpleNamespace(logic=host))
    for attribute in {
        attr
        for attrs in LogicThread._RUNTIME_HOST_CONTRACTS.values()
        for attr in attrs
    }:
        if not hasattr(host, attribute):
            setattr(host, attribute, None)
    return host


def test_logic_thread_runtime_contract_validation_catches_missing_host_state():
    host = _contract_host()

    host._validate_runtime_contracts()

    del host.player

    with pytest.raises(AssertionError, match="player"):
        host._validate_runtime_contracts()
