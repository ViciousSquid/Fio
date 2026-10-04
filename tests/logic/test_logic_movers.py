"""Focused unit tests for the extracted LogicMovers runtime."""

from types import SimpleNamespace

import pytest

from engine.logic_movers import LogicMovers


def _host(brushes):
    host = SimpleNamespace(
        editor_state=SimpleNamespace(brushes=brushes, things=[]),
        movers=[],
        doors=[],
        mover_path_states={},
        _mover_brush_list=[],
        _door_brush_list=[],
        io_manager=None,
        _plugin_emit=lambda *args, **kwargs: None,
    )
    return host


def test_logic_movers_initializes_repeatable_mover_state():
    mover = {"name": "lift", "is_mover": True, "pos": [0, 10, 0]}
    host = _host([mover])
    runtime = LogicMovers(host)

    runtime._init_movers()

    assert host.movers == [(0, mover)]
    assert runtime.mover_states[0] == {"progress": 0.0, "forward": True}
    assert mover["original_pos"] == [0, 10, 0]


def test_logic_movers_does_not_create_repeat_state_for_move_once():
    mover = {
        "name": "lift",
        "is_mover": True,
        "move_once": True,
        "pos": [0, 0, 0],
    }
    host = _host([mover])
    runtime = LogicMovers(host)

    runtime._init_movers()

    assert host.movers == [(0, mover)]
    assert runtime.mover_states == {}


def test_logic_movers_initializes_door_direction_and_distance():
    door = {
        "name": "gate",
        "is_door": True,
        "pos": [0, 0, 0],
        "door_direction": "east",
        "door_speed": 64,
        "door_distance": 128,
        "door_lip": 8,
    }
    host = _host([door])
    runtime = LogicMovers(host)

    runtime._init_doors()

    state = runtime.door_states[0]
    assert state["state"] == "closed"
    assert state["speed"] == pytest.approx(64.0)
    assert state["distance"] == pytest.approx(120.0)
    assert state["direction"] == [1, 0, 0]


def test_logic_movers_unknown_door_direction_defaults_up():
    door = {"is_door": True, "pos": [0, 0, 0], "door_direction": "nonsense"}
    host = _host([door])
    runtime = LogicMovers(host)

    runtime._init_doors()

    assert runtime.door_states[0]["direction"] == [0, 1, 0]


def test_logic_movers_reset_restores_original_mover_position():
    mover = {"is_mover": True, "pos": [10, 20, 30]}
    host = _host([mover])
    runtime = LogicMovers(host)
    runtime._init_movers()

    mover["pos"] = [100, 200, 300]
    runtime._reset_movers()

    assert mover["pos"] == [10, 20, 30]
    assert host.movers == []
    assert runtime.mover_states == {}


def test_logic_movers_reset_restores_original_door_position():
    door = {"is_door": True, "pos": [1, 2, 3]}
    host = _host([door])
    runtime = LogicMovers(host)
    runtime._init_doors()

    door["pos"] = [50, 60, 70]
    runtime._reset_doors()

    assert door["pos"] == [1, 2, 3]
    assert host.doors == []
    assert runtime.door_states == {}


def test_logic_movers_open_trigger_moves_closed_door_to_opening():
    door = {"is_door": True, "pos": [0, 0, 0]}
    host = _host([door])
    runtime = LogicMovers(host)
    runtime._init_doors()

    runtime._trigger_door_open(0, door)

    assert runtime.door_states[0]["state"] == "opening"


def test_logic_movers_open_trigger_does_not_restart_already_open_door():
    door = {"is_door": True, "pos": [0, 0, 0]}
    host = _host([door])
    runtime = LogicMovers(host)
    runtime._init_doors()
    runtime.door_states[0]["state"] = "open"

    runtime._trigger_door_open(0, door)

    assert runtime.door_states[0]["state"] == "open"
