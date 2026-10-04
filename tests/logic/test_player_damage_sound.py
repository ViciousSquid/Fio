"""Player damage should queue a spatial pain sound at the damage location."""

from types import SimpleNamespace

import glm
import pytest

from engine.logic_triggers import LogicTriggers


def _logic(player_pos=(10.0, 20.0, 30.0), health=100):
    logic = SimpleNamespace()
    logic._player_damage_lock = __import__("threading").Lock()
    logic.player_runtime = SimpleNamespace(
        god_mode=False,
        buddha_mode=False,
        notarget=False,
    )
    logic.player_health = health
    logic.player = SimpleNamespace(pos=glm.vec3(*player_pos))
    logic.game_state = SimpleNamespace(sounds=[])

    def queue_sound(request):
        logic.game_state.sounds.append(dict(request))

    logic.game_state.queue_sound = queue_sound
    logic.events = []

    def emit(event, **data):
        logic.events.append((event, data))

    logic._plugin_emit = emit
    return logic


def test_player_damage_queues_one_spatial_pain_sound():
    logic = _logic(player_pos=(10.0, 20.0, 30.0))

    LogicTriggers(logic)._apply_player_damage(10)

    assert logic.player_health == 90
    assert len(logic.game_state.sounds) == 1
    request = logic.game_state.sounds[0]
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
    logic.player.pos = glm.vec3(100.0, 200.0, 300.0)

    assert logic.game_state.sounds[0]["position"] == (1.0, 2.0, 3.0)


def test_no_pain_sound_when_damage_is_ignored():
    logic = _logic()

    logic.player_runtime.god_mode = True
    LogicTriggers(logic)._apply_player_damage(10)

    assert logic.player_health == 100
    assert logic.game_state.sounds == []
