"""Mover and door runtime delegated from LogicThread.

LogicThread remains the simulation orchestrator. This module owns
mover/door initialization, state-table access, row reindexing, path-following,
door activation, and per-tick advancement.
"""

from __future__ import annotations

import glm
import numpy as np

from .change_journal import moved
from .mover_table import MoverTable

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


# Map door_direction editor strings to movement vectors.
DOOR_DIRECTION_MAP = {
    "up":    [0,  1,  0],
    "down":  [0, -1,  0],
    "north": [0,  0,  1],
    "south": [0,  0, -1],
    "east":  [1,  0,  0],
    "west":  [-1, 0,  0],
}


class LogicMovers:
    """Mover and door subsystem for a single LogicThread host."""

    def __init__(self, logic):
        self.logic = logic
        self._mover_table = MoverTable()
        self._moving_rows = None
        self.movers = []
        self.doors = []
        self.mover_path_states = {}
        self._mover_brush_list = []
        self._door_brush_list = []

    def _movers(self):
        """Return the dense mover table owned by this runtime."""
        return self._mover_table

    @property
    def mover_states(self):
        """Return the mover state mapping view."""
        return self._movers().movers.states

    @mover_states.setter
    def mover_states(self, states):
        self._movers().movers.replace_states(self.movers, states)

    @property
    def door_states(self):
        """Return the door state mapping view."""
        return self._movers().doors.states

    @door_states.setter
    def door_states(self, states):
        self._movers().doors.replace_states(self.doors, states)

    def _init_movers(self):
        logic = self.logic
        self.mover_path_states = {}
        self.movers = []
        states = {}
        for i, brush in enumerate(logic.editor_state.brushes):
            if brush.get("is_mover"):
                self.movers.append((i, brush))
                if "original_pos" not in brush:
                    brush["original_pos"] = list(brush["pos"])

                if brush.get("rotate", False) and "rotation_yaw" not in brush:
                    brush["rotation_yaw"] = 0.0

                path_target = brush.get("path_target", "")
                if path_target and brush.get("start_on", False):
                    self.mover_path_states[i] = {
                        "current_node": path_target,
                        "lerp_t": 0.0,
                        "origin": list(brush["pos"]),
                        "waiting": False,
                        "wait_remaining": 0.0,
                    }
                elif not brush.get("move_once", False):
                    states[i] = {"progress": 0.0, "forward": True}

        self.mover_states = states
        self._mover_brush_list = [b for _, b in self.movers]

    def _reset_movers(self):
        logic = self.logic
        self.movers = []
        for _i, brush in enumerate(logic.editor_state.brushes):
            if brush.get("is_mover") and "original_pos" in brush:
                brush["pos"] = list(brush["original_pos"])
                moved(brush)
        self.mover_states = {}
        self._mover_brush_list = []

    def _init_doors(self):
        logic = self.logic
        self.doors = []
        states = {}
        for i, brush in enumerate(logic.editor_state.brushes):
            if brush.get("is_door"):
                speed = float(brush.get("door_speed", brush.get("speed", 128.0)))
                distance = float(brush.get("door_distance", brush.get("distance", 128.0)))
                dir_str = brush.get("door_direction", "")
                direction = DOOR_DIRECTION_MAP.get(dir_str, [0, 1, 0])

                if "door_lip" in brush:
                    lip = float(brush.get("door_lip", 0.0))
                    distance = max(1.0, distance - lip)

                self.doors.append((i, brush))
                if "original_pos" not in brush:
                    brush["original_pos"] = list(brush["pos"])

                states[i] = {
                    "progress": 0.0,
                    "state": "closed",
                    "open_timer": 0.0,
                    "speed": speed,
                    "distance": distance,
                    "direction": direction,
                    "_direction_np": (
                        float(direction[0]),
                        float(direction[1]),
                        float(direction[2]),
                    ),
                }

        self.door_states = states
        self._door_brush_list = [b for _, b in self.doors]
        self._moving_rows = tuple(logic.editor_state.brushes)

    def _reset_doors(self):
        logic = self.logic
        self.doors = []
        for _i, brush in enumerate(logic.editor_state.brushes):
            if brush.get("is_door") and "original_pos" in brush:
                brush["pos"] = list(brush["original_pos"])
                moved(brush)
        self.door_states = {}
        self._door_brush_list = []

    def _trigger_door_open(self, door_idx: int, brush: dict):
        """Start opening a door if it is currently closed or closing."""
        logic = self.logic
        if door_idx not in self.door_states:
            return
        state = self.door_states[door_idx]
        if state["state"] in ("closed", "closing"):
            state["state"] = "opening"
            if logic.io_manager:
                logic.io_manager.fire_output(brush, "OnOpen")
            logic._plugin_emit("door_open", door=brush, door_idx=door_idx)

    def _reindex_moving_brushes(self):
        """Re-key mover and door state after the brush list changes in play.

        States are keyed by brush index, but edits can insert or delete brushes
        before a moving row. Surviving rows keep their runtime state by object
        identity; newly added movers and doors use normal initial state.
        """
        logic = self.logic
        movers, doors = self.movers, self.doors
        m_states, d_states = self.mover_states, self.door_states
        paths = self.mover_path_states

        kept_m = {
            id(brush): (
                dict(m_states[index]) if index in m_states else None,
                paths.get(index),
            )
            for index, brush in movers
        }
        kept_d = {
            id(brush): dict(d_states[index]) if index in d_states else None
            for index, brush in doors
        }

        self._init_movers()
        self._init_doors()

        m_new = {index: dict(state) for index, state in self.mover_states.items()}
        for index, brush in self.movers:
            if id(brush) in kept_m:
                state, path = kept_m[id(brush)]
                m_new.pop(index, None)
                self.mover_path_states.pop(index, None)
                if state is not None:
                    m_new[index] = state
                if path is not None:
                    self.mover_path_states[index] = path
        self.mover_states = m_new

        d_new = {index: dict(state) for index, state in self.door_states.items()}
        for index, brush in self.doors:
            if id(brush) in kept_d:
                d_new.pop(index, None)
                if kept_d[id(brush)] is not None:
                    d_new[index] = kept_d[id(brush)]
        self.door_states = d_new

    def _update_movers(self, delta: float):
        """Advance every mover one tick."""
        self._movers().tick_movers(self.logic, delta)

    def _update_mover_path(self, idx: int, brush: dict, delta: float):
        logic = self.logic
        state = self.mover_path_states[idx]
        node_name = state["current_node"]
        if not node_name:
            return

        node = logic.world_runtime.find_path_node_by_name(node_name)
        if node is None:
            debug_log("IO", f"Mover path: node '{node_name}' not found — stopping")
            self.mover_path_states.pop(idx, None)
            return

        if state["waiting"]:
            state["wait_remaining"] -= delta
            if state["wait_remaining"] <= 0.0:
                state["waiting"] = False
                next_name = node.get_next_node_name()
                if next_name:
                    state["origin"] = list(brush["pos"])
                    state["current_node"] = next_name
                    state["lerp_t"] = 0.0
                else:
                    brush["start_on"] = False
                    if logic.io_manager:
                        logic.io_manager.fire_output(brush, "OnFullyClosed")
                    self.mover_path_states.pop(idx, None)
            return

        origin = np.array(state["origin"], dtype=float)
        target = np.array(node.pos, dtype=float)
        segment_vec = target - origin
        segment_len = np.linalg.norm(segment_vec)

        if segment_len < 1.0:
            state["lerp_t"] = 1.0
        else:
            speed = brush.get("speed", 64.0) * node.get_speed()
            state["lerp_t"] += (speed * delta) / segment_len

        if state["lerp_t"] >= 1.0:
            state["lerp_t"] = 1.0
            new_pos = target
            move_delta = new_pos - np.array(brush["pos"])
            brush["pos"] = new_pos.tolist()

            if logic.player_runtime.player and logic.player.ground_object == brush:
                logic.player.pos += glm.vec3(
                    float(move_delta[0]),
                    float(move_delta[1]),
                    float(move_delta[2]),
                )

            if logic.io_manager:
                logic.io_manager.fire_output(
                    brush, "OnPathNodeReached", value=node_name
                )
                logic.io_manager.fire_output(brush, "OnFullyOpen")

            wait_time = node.get_wait_time()
            if wait_time > 0.0:
                state["waiting"] = True
                state["wait_remaining"] = wait_time
            else:
                next_name = node.get_next_node_name()
                if next_name:
                    state["origin"] = list(brush["pos"])
                    state["current_node"] = next_name
                    state["lerp_t"] = 0.0
                else:
                    if logic.io_manager:
                        logic.io_manager.fire_output(brush, "OnFullyClosed")
                    self.mover_path_states.pop(idx, None)
        else:
            t = state["lerp_t"]
            eased = (
                4 * t * t * t
                if t < 0.5
                else 1 - pow(-2 * t + 2, 3) / 2
            )
            new_pos = origin + segment_vec * eased
            move_delta = new_pos - np.array(brush["pos"])
            brush["pos"] = new_pos.tolist()

            if logic.player and logic.player.ground_object == brush:
                logic.player.pos += glm.vec3(
                    float(move_delta[0]),
                    float(move_delta[1]),
                    float(move_delta[2]),
                )

    def _update_doors(self, delta: float):
        """Advance every door one tick."""
        self._movers().tick_doors(self.logic, delta)
