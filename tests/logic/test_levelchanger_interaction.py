"""Vectorised LevelChanger interaction coverage through the production logic owner."""

import numpy as np
import pytest
import glm

pytest.importorskip("PyQt5", reason="LevelChanger entities require the Qt-backed editor things")

from editor.editor_state import EditorState
from editor.things import LevelChanger
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState
from tests.helpers.worlds import make_thing


@pytest.fixture
def logic(request):
    state = EditorState()
    logic = LogicThread(ThreadedGameState(), state)
    request.addfinalizer(logic.stop)
    return logic


def _levelchanger(name="LevelChanger_1", pos=(0.0, 0.0, 0.0), radius=128.0, **props):
    values = {
        "target_map": "NextMap",
        "radius": radius,
        "usable": True,
        "disabled": False,
    }
    values.update(props)
    return make_thing(LevelChanger, name, pos, **values)


def _prepare(logic, things, player_pos=(0.0, 0.0, 96.0), angle=np.pi):
    logic.editor_state.things = list(things)
    logic.editor_state.brushes = []
    # The player exists from Play on, as QtGameView.toggle_play_mode makes it.
    from engine.player import Player
    logic.player_runtime.player = Player(player_pos[0], player_pos[2], float(angle))
    logic.player_runtime.player.pos = glm.vec3(*player_pos)
    logic.world_runtime.build_entity_caches()


def test_levelchanger_activation_uses_float32_dense_columns(logic):
    _prepare(logic, [
        _levelchanger(pos=(0.0, 0.0, 0.0), radius=128.0),
        _levelchanger(name="LevelChanger_2", pos=(500.0, 0.0, 0.0), radius=64.0),
    ])

    assert logic.world_runtime.levelchanger_centres.shape == (2, 3)
    assert logic.world_runtime.levelchanger_centres.dtype == np.float32
    assert logic.world_runtime.levelchanger_radii.shape == (2,)
    assert logic.world_runtime.levelchanger_radii.dtype == np.float32
    assert logic.world_runtime.levelchanger_eligible.dtype == np.bool_


def test_levelchanger_radius_boundary_is_squared_without_glm_distance(logic):
    _prepare(
        logic,
        [_levelchanger(radius=100.0)],
        player_pos=(80.0, 0.0, 80.0),
        angle=np.pi + np.pi / 4,
    )

    logic.interaction_runtime.handle(False)
    assert logic.interaction_runtime.current_hud_message == ""

    logic.player_runtime.player.pos = np.asarray((60.0, 0.0, 60.0), dtype=np.float32)
    logic.interaction_runtime.handle(False)
    assert logic.interaction_runtime.current_hud_message == "[E] Complete Level"


def test_levelchanger_first_matching_row_wins_after_vectorised_filter(logic):
    first = _levelchanger(name="First", radius=128.0)
    second = _levelchanger(name="Second", radius=128.0)
    _prepare(logic, [first, second])

    fired = []
    real_fire_output = logic.io_manager.fire_output

    def record(entity, output_name, value=None, activator_entity=None):
        fired.append((entity.properties["name"], output_name))
        return real_fire_output(
            entity, output_name, value=value, activator_entity=activator_entity
        )

    logic.io_manager.fire_output = record
    logic.interaction_runtime.handle(True)

    assert logic.interaction_runtime.current_hud_message == "[E] Complete Level"
    # The map as the LevelChanger's ChangeLevel input resolves it, and the
    # start there (its primary: no destination spawn is set).
    assert logic.interaction_runtime.level_complete_ui["target_map"] == "maps/NextMap.json"
    assert logic.interaction_runtime.level_complete_ui["destination_spawn"] == ""
    assert fired == [("First", "OnUse")]
