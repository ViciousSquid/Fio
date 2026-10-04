"""Player damage should queue a spatial pain sound at the damage location."""

import glm
import pytest

pytest.importorskip("PyQt5", reason="LogicThread uses the editor world state")

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.logic_triggers import LogicTriggers
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt


def _logic(player_pos=(10.0, 20.0, 30.0), health=100):
    logic = LogicThread(ThreadedGameState(), EditorState())
    logic.player_runtime.player = Player(player_pos[0], player_pos[2])
    logic.player_runtime.player.pos.y = player_pos[1]
    logic.player_runtime.player_health = health
    return logic


def test_player_damage_queues_one_spatial_pain_sound():
    logic = _logic(player_pos=(10.0, 20.0, 30.0))

    LogicTriggers(logic)._apply_player_damage(10)

    assert logic.player_runtime.player_health == 90
    sounds = logic.game_state.consume_sounds()
    assert len(sounds) == 1
    request = sounds[0]
    assert request["file"] in {
        "assets/sounds/pain01.mp3",
        "assets/sounds/pain02.mp3",
        "assets/sounds/pain03.mp3",
    }
    assert request["volume"] == 1.0
    assert request["position"] == (10.0, 20.0, 30.0)
    assert request["radius"] == 512.0


def test_pain_sound_position_is_a_snapshot():
    logic = _logic(player_pos=(1.0, 2.0, 3.0))

    LogicTriggers(logic)._apply_player_damage(10)
    logic.player_runtime.player.pos = glm.vec3(100.0, 200.0, 300.0)

    sounds = logic.game_state.consume_sounds()
    assert sounds[0]["position"] == (1.0, 2.0, 3.0)


def test_no_pain_sound_when_damage_is_ignored():
    logic = _logic()

    logic.player_runtime.god_mode = True
    LogicTriggers(logic)._apply_player_damage(10)

    assert logic.player_runtime.player_health == 100
    assert logic.game_state.consume_sounds() == ()
