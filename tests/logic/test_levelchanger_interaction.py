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
    from engine.logic_interaction import LogicInteraction
    from engine.logic_movers import LogicMovers
    from engine.logic_world import LogicWorld

    logic = LogicThread.__new__(LogicThread)
    logic.play_mode = False
    logic.editor_state = SimpleNamespace(things=list(things), brushes=[])
    logic.player = SimpleNamespace(
        pos=list(player_pos),
        angle=float(angle),
    )
    logic.io_manager = None
    logic.level_complete_ui = None
    logic.mover_runtime = LogicMovers(logic)
    logic.world_runtime = LogicWorld(logic, levelchanger_type=type(things[0]))
    logic.world_runtime.levelchanger_things = list(things)
    logic.world_runtime.refresh_levelchanger_table()
    logic.interaction_runtime = LogicInteraction(logic)
    return logic


def test_levelchanger_activation_uses_float32_dense_columns():
    logic = _logic([
        _levelchanger(pos=(0.0, 0.0, 0.0), radius=128.0),
        _levelchanger(name="LevelChanger_2", pos=(500.0, 0.0, 0.0), radius=64.0),
    ])

    assert logic.world_runtime.levelchanger_centres.shape == (2, 3)
    assert logic.world_runtime.levelchanger_centres.dtype == np.float32
    assert logic.world_runtime.levelchanger_radii.shape == (2,)
    assert logic.world_runtime.levelchanger_radii.dtype == np.float32
    assert logic.world_runtime.levelchanger_eligible.dtype == np.bool_


def test_levelchanger_radius_boundary_is_squared_without_glm_distance():
    logic = _logic([
        _levelchanger(radius=100.0),
    ], player_pos=(80.0, 0.0, 80.0), angle=np.pi + np.pi / 4)

    logic.interaction_runtime.handle(False)
    assert logic.interaction_runtime.current_hud_message == ""

    logic.player.pos = [60.0, 0.0, 60.0]
    logic.interaction_runtime.handle(False)
    assert logic.interaction_runtime.current_hud_message == "[E] Complete Level"


def test_levelchanger_first_matching_row_wins_after_vectorised_filter():
    first = _levelchanger(name="First", radius=128.0)
    second = _levelchanger(name="Second", radius=128.0)
    logic = _logic([first, second])

    fired = []
    logic.io_manager = SimpleNamespace(
        fire_output=lambda entity, output: fired.append((entity.properties["name"], output))
    )

    logic.interaction_runtime.handle(True)

    assert logic.interaction_runtime.current_hud_message == "[E] Complete Level"
    assert logic.interaction_runtime.level_complete_ui["target_map"] == "NextMap"
    assert fired == [("First", "OnUse")]
