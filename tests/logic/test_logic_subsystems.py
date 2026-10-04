"""Logic subsystem coverage through the production LogicThread owner.

These tests deliberately exercise the runtime objects as LogicThread constructs
and owns them. Authored brushes/entities are supplied as data; the host/runtime
objects are never replaced with namespace doubles.
"""

import numpy as np
import pytest
import glm

pytest.importorskip("PyQt5", reason="the production LogicThread uses Qt-backed editor entities")

from editor.editor_state import EditorState
from editor.things import Light, LevelChanger, Portal
from engine.logic_thread import LogicThread
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState
from tests.helpers.worlds import make_thing


@pytest.fixture
def logic(request):
    state = EditorState()
    logic = LogicThread(ThreadedGameState(), state)
    request.addfinalizer(logic.stop)
    return logic


def _player_at(logic, pos=(0.0, 0.0, 0.0), angle=0.0):
    player = logic.player_runtime.player
    player.pos = glm.vec3(*pos)
    player.angle = float(angle)
    return player


def test_logic_camera_computes_overhead_footprint_from_the_real_camera(logic):
    player = _player_at(logic)
    player.pos = glm.vec3(0, 0, 0)
    logic.camera.set_camera_mode("overhead")
    logic.camera.overhead_height = 400.0

    footprint = logic.camera.overhead_ground_footprint()

    assert logic.camera.is_overhead()
    assert footprint is not None
    assert all(value > 0.0 for value in footprint)


def test_logic_collision_uses_the_real_collision_runtime(logic):
    runtime = logic.collision_runtime
    logic.editor_state.brushes = []

    assert runtime.angled_brush_is_solid({"geometry": {}}) is True
    assert runtime.angled_brush_is_solid({"hidden": True}) is False
    assert runtime.angled_brush_is_solid({"is_trigger": True}) is False
    assert runtime.toggle_model_collision(False) is False
    assert runtime.toggle_model_collision(True) is True


def test_logic_combat_ray_tests_run_on_the_real_combat_runtime(logic):
    hit, distance = logic.combat_runtime.intersect_ray_aabb(
        (0, 0, 0),
        (1, 0, 0),
        (5, -1, -1),
        (6, 1, 1),
    )

    assert hit is True
    assert distance == pytest.approx(5.0)


def test_logic_editor_processes_an_idle_tick_through_the_real_runtime(logic):
    logic.camera.set_editor_camera(
        glm.vec3(0, 0, 0), 0.0, 0.0, logic.camera.editor_camera.fov
    )

    logic.editor_runtime.tick(1.0 / 60.0)

    assert logic.camera.get_editor_camera().pos == glm.vec3(0, 0, 0)


def test_logic_interaction_opens_a_real_door_runtime(logic):
    door = {
        "pos": [0, 0, 0],
        "size": [32, 64, 32],
        "is_door": True,
        "door_locked": False,
    }
    logic.editor_state.brushes = [door]
    logic.editor_state.things = []
    _player_at(logic)

    logic.mover_runtime._init_doors()
    logic.interaction_runtime.handle(True)

    assert logic.mover_runtime.door_states[0]["state"] == "opening"
    assert logic.interaction_runtime.current_hud_message == "[E] Open"


def test_logic_movers_indexes_real_editor_brushes(logic):
    mover = {
        "name": "lift",
        "is_mover": True,
        "pos": [0, 0, 0],
    }
    logic.editor_state.brushes = [mover]
    logic.editor_state.things = []

    logic.mover_runtime._init_movers()

    assert logic.mover_runtime.movers == [(0, mover)]
    assert logic.mover_runtime.mover_states[0]["progress"] == pytest.approx(0.0)
    assert mover["original_pos"] == [0, 0, 0]


def test_logic_parenting_updates_a_real_light_entity(logic):
    brush = {"name": "lift", "is_mover": True, "pos": [0, 0, 0]}
    light = make_thing(Light, "lamp", (4, 2, 0), parent_mover="lift")
    logic.editor_state.brushes = [brush]
    logic.editor_state.things = [light]

    logic.parenting_runtime._init_parented_lights()
    brush["pos"] = [10, 5, 20]
    logic.parenting_runtime._update_parented_lights()

    assert np.allclose(light.pos, [14, 7, 20])
    assert len(logic.parenting_runtime._parented_lights) == 1


def test_logic_player_reports_water_transition_through_real_player(logic):
    player = _player_at(logic)
    player.in_water = True
    player.swimming = False
    player.on_ground = True
    player.velocity = glm.vec3(30, 0, 0)

    logic.player_runtime.update_water_sounds(0.1)

    assert [sound["file"] for sound in logic.game_state.sounds] == [
        "enterwater.wav",
        "waterwalk.wav",
    ]
    assert logic.player_runtime._player_was_in_water is True


def test_logic_portals_rebuild_real_portal_target_links(logic):
    first = make_thing(Portal, "A", (0, 0, 0), portal_target="B")
    second = make_thing(Portal, "B", (100, 0, 0), portal_target="A")
    logic.editor_state.things = [first, second]
    logic.editor_state.brushes = []

    logic.portal_runtime.rebuild_links()

    assert logic.portal_runtime.portal_slots.tolist() == [0, 1]
    assert logic.portal_runtime.portal_target_slots.tolist() == [1, 0]
    assert logic.portal_runtime.portal_target_things == [second, first]


def test_logic_render_batches_frustum_tests_on_the_real_runtime(logic):
    plane = logic.render_runtime.normalize_plane(2.0, 0.0, 0.0, -2.0)
    visible = logic.render_runtime.aabb_in_frustum_batch(
        [plane],
        [[2.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
        [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]],
    )

    assert plane == pytest.approx((1.0, 0.0, 0.0, -1.0))
    assert np.array_equal(visible, np.array([True, False]))


def test_logic_session_releases_real_session_cache_state(logic):
    player = _player_at(logic)
    player.ground_object = object()

    logic.collision_runtime._collision_brushes_cache = [1]
    logic.collision_runtime._model_collision_brushes = [2]
    logic.collision_runtime._physics_body_brushes = [3]
    logic.mover_runtime._mover_brush_list = [4]
    logic.mover_runtime._door_brush_list = [5]
    logic.world_runtime.monster_spawn_health = {"monster": 100}

    logic.session_runtime.release_session_caches()

    assert player.ground_object is None
    assert logic.collision_runtime._collision_brushes_cache == []
    assert logic.world_runtime.monster_spawn_health == {}


def test_logic_timing_advances_a_real_light_fade(logic):
    light = make_thing(Light, "lamp", (0, 0, 0), intensity=0.0)
    logic.world_runtime.timer_things = []

    logic.timing_runtime.light_fade_states["lamp"] = {
        "entity": light,
        "elapsed": 0.0,
        "duration": 1.0,
        "from": 0.0,
        "to": 1.0,
        "end_off": True,
    }

    logic.timing_runtime.update_light_fades(1.0)

    assert light.properties["intensity"] == pytest.approx(1.0)
    assert light.properties["state"] == "off"
    assert logic.timing_runtime.light_fade_states == {}


def test_logic_triggers_own_hurt_cadence_on_the_real_runtime(logic):
    runtime = logic.trigger_runtime

    assert runtime.HURT_INTERVAL == pytest.approx(0.5)
    assert not hasattr(runtime.logic, "HURT_INTERVAL")
    assert runtime._trigger_filters({"trigger_filters": "Player"}) == {"player"}

    inside = runtime.use_trigger_contains(
        np.array([3.0, 9.0]),
        np.array([4.0, 2.0]),
    )
    assert np.array_equal(inside, np.array([True, False]))


def test_logic_world_packs_real_levelchanger_rows(logic):
    first = make_thing(LevelChanger, "first", (1, 2, 3), radius=32)
    second = make_thing(LevelChanger, "second", (4, 5, 6), radius=64, disabled=True)
    logic.editor_state.things = [first, second]
    logic.editor_state.brushes = []

    logic.world_runtime.refresh_levelchanger_table()
    logic.world_runtime.build_entity_caches()

    assert logic.world_runtime.name_cache["first"] is first
    assert logic.world_runtime.levelchanger_centres.dtype == np.float32
    assert np.array_equal(
        logic.world_runtime.levelchanger_centres,
        np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float32),
    )
    assert np.array_equal(
        logic.world_runtime.levelchanger_radii,
        np.array([32, 64], dtype=np.float32),
    )
    assert np.array_equal(
        logic.world_runtime.levelchanger_eligible,
        np.array([True, False]),
    )


def test_logic_thread_runtime_contract_validation_uses_the_real_owner(logic):
    logic._validate_runtime_contracts()

    del logic.player_runtime

    with pytest.raises(AssertionError, match="player_runtime"):
        logic._validate_runtime_contracts()
