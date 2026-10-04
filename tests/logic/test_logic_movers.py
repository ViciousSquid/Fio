"""Focused LogicMovers coverage through the production LogicThread owner."""

import pytest

pytest.importorskip("PyQt5", reason="LogicThread owns Qt-backed entity types")

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState


@pytest.fixture
def logic(request):
    state = EditorState()
    logic = LogicThread(ThreadedGameState(), state)
    request.addfinalizer(logic.stop)
    return logic


def _brush(logic, value):
    logic.editor_state.brushes = [value]
    logic.editor_state.things = []
    return logic.mover_runtime


def test_logic_movers_initializes_repeatable_mover_state(logic):
    mover = {"name": "lift", "is_mover": True, "pos": [0, 10, 0]}
    runtime = _brush(logic, mover)
    runtime._init_movers()

    assert runtime.movers == [(0, mover)]
    assert runtime.mover_states[0] == {"progress": 0.0, "forward": True}
    assert mover["original_pos"] == [0, 10, 0]


def test_logic_movers_does_not_create_repeat_state_for_move_once(logic):
    mover = {
        "name": "lift",
        "is_mover": True,
        "move_once": True,
        "pos": [0, 0, 0],
    }
    runtime = _brush(logic, mover)
    runtime._init_movers()

    assert runtime.movers == [(0, mover)]
    assert runtime.mover_states == {}


def test_logic_movers_initializes_door_direction_and_distance(logic):
    door = {
        "name": "gate",
        "is_door": True,
        "pos": [0, 0, 0],
        "door_direction": "east",
        "door_speed": 64,
        "door_distance": 128,
        "door_lip": 8,
    }
    runtime = _brush(logic, door)
    runtime._init_doors()

    state = runtime.door_states[0]
    assert state["state"] == "closed"
    assert state["speed"] == pytest.approx(64.0)
    assert state["distance"] == pytest.approx(120.0)
    assert state["direction"] == [1, 0, 0]


def test_logic_movers_unknown_door_direction_defaults_up(logic):
    door = {"is_door": True, "pos": [0, 0, 0], "door_direction": "nonsense"}
    runtime = _brush(logic, door)
    runtime._init_doors()

    assert runtime.door_states[0]["direction"] == [0, 1, 0]


def test_logic_movers_reset_restores_original_mover_position(logic):
    mover = {"is_mover": True, "pos": [10, 20, 30]}
    runtime = _brush(logic, mover)
    runtime._init_movers()

    mover["pos"] = [100, 200, 300]
    runtime._reset_movers()

    assert mover["pos"] == [10, 20, 30]
    assert runtime.movers == []
    assert runtime.mover_states == {}


def test_logic_movers_reset_restores_original_door_position(logic):
    door = {"is_door": True, "pos": [1, 2, 3]}
    runtime = _brush(logic, door)
    runtime._init_doors()

    door["pos"] = [50, 60, 70]
    runtime._reset_doors()

    assert door["pos"] == [1, 2, 3]
    assert runtime.doors == []
    assert runtime.door_states == {}


def test_logic_movers_open_trigger_moves_closed_door_to_opening(logic):
    door = {"is_door": True, "pos": [0, 0, 0]}
    runtime = _brush(logic, door)
    runtime._init_doors()

    runtime._trigger_door_open(0, door)

    assert runtime.door_states[0]["state"] == "opening"


def test_logic_movers_open_trigger_does_not_restart_already_open_door(logic):
    door = {"is_door": True, "pos": [0, 0, 0]}
    runtime = _brush(logic, door)
    runtime._init_doors()
    runtime.door_states[0]["state"] = "open"

    runtime._trigger_door_open(0, door)

    assert runtime.door_states[0]["state"] == "open"
