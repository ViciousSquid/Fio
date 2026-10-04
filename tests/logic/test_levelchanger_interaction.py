"""Vectorised LevelChanger interaction coverage."""

from types import SimpleNamespace

import numpy as np

import engine.logic_thread as logic_thread
from engine.logic_thread import LogicThread


def _levelchanger(name="LevelChanger_1", pos=(0.0, 0.0, 0.0), radius=128.0, **props):
    values = {
        "type": "levelchanger",
        "name": name,
        "target_map": "NextMap",
        "radius": radius,
        "usable": True,
        "disabled": False,
    }
    values.update(props)
    return SimpleNamespace(
        pos=list(pos),
        properties=values,
    )


def _logic(things, player_pos=(0.0, 0.0, 96.0), angle=np.pi):
    logic = LogicThread.__new__(LogicThread)
    logic._levelchanger_things = list(things)
    from engine.logic_world import LogicWorld
    from engine.logic_interaction import LogicInteraction
    logic.world_runtime = LogicWorld(logic)
    logic.world_runtime.refresh_levelchanger_table()
    logic.player = SimpleNamespace(
        pos=list(player_pos),
        angle=float(angle),
    )
    logic.doors = []
    logic.door_states = {}
    logic.collected_keys = set()
    logic.current_hud_message = ""
    logic.current_hud_key_name = None
    logic.io_manager = None
    logic.level_complete_ui = None
    return logic


def test_levelchanger_activation_uses_float32_dense_columns():
    logic = _logic([
        _levelchanger(pos=(0.0, 0.0, 0.0), radius=128.0),
        _levelchanger(name="LevelChanger_2", pos=(500.0, 0.0, 0.0), radius=64.0),
    ])

    assert logic._levelchanger_centres.shape == (2, 3)
    assert logic._levelchanger_centres.dtype == np.float32
    assert logic._levelchanger_radii.shape == (2,)
    assert logic._levelchanger_radii.dtype == np.float32
    assert logic._levelchanger_eligible.dtype == np.bool_


def test_levelchanger_radius_boundary_is_squared_without_glm_distance(monkeypatch):
    logic = _logic([
        _levelchanger(radius=100.0),
    ], player_pos=(80.0, 0.0, 80.0), angle=np.pi + np.pi / 4)

    monkeypatch.setattr(
        logic_thread.glm,
        "distance",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("glm.distance must not be used by LevelChanger activation")
        ),
    )

    logic.interaction_runtime.handle(False)
    assert logic.current_hud_message == ""

    logic.player.pos = [60.0, 0.0, 60.0]
    logic.interaction_runtime.handle(False)
    assert logic.current_hud_message == "[E] Complete Level"


def test_levelchanger_first_matching_row_wins_after_vectorised_filter():
    first = _levelchanger(name="First", radius=128.0)
    second = _levelchanger(name="Second", radius=128.0)
    logic = _logic([first, second])

    fired = []
    logic.io_manager = SimpleNamespace(
        fire_output=lambda entity, output: fired.append((entity.properties["name"], output))
    )

    logic.interaction_runtime.handle(True)

    assert logic.current_hud_message == "[E] Complete Level"
    assert logic.level_complete_ui["target_map"] == "NextMap"
    assert fired == [("First", "OnUse")]
