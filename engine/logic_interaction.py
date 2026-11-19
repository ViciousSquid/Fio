"""Player-to-world interaction handling delegated from LogicThread.

Owns the per-tick door/LevelChanger interaction query and prompt generation.
LogicThread remains the authoritative tick-order orchestrator.
"""

from __future__ import annotations

import math

import numpy as np


#: Level changers from which the facing query runs in NumPy (a small-N
#: crossover: tests/performance/test_small_n_invariant.py). Below it a Python
#: loop: a map has one to three level changers, and NumPy's fixed cost a call
#: (the offsets, einsum, two masks, flatnonzero) is paid every tick however
#: few rows there are. Measured, microseconds a query on x86 (re-measure:
#: python -m pytest tests/performance/test_levelchanger_crossover_benchmark.py
#: -s --run-benchmarks):
#:
#:     rows    loop    NumPy
#:       1     0.58     4.37
#:       4     0.99     6.14
#:      16     2.20     9.34
#:      64     6.13    15.39
#:     128     9.63    12.48
#:     192    14.01    11.15
#:     256    17.13    12.17
#:
#: They cross between 128 and 192 rows. Both paths give the same row (the
#: authored order decides: the first facing changer in range wins).
LEVELCHANGER_VECTOR_FROM = 160


def facing_levelchanger_row(centres, radii, eligible, pos, angle):
    """The first level changer (row) the player at *pos* facing *angle* is
    in range of and facing, or -1. In range: within its radius (squared, no
    sqrt). Facing: in front, within 60 degrees of straight ahead."""
    n = len(centres)
    if n == 0:
        return -1
    fx, fz = math.sin(angle), math.cos(angle)
    if n < LEVELCHANGER_VECTOR_FROM:
        px, py, pz = (float(v) for v in pos)
        rows = centres.tolist()
        radius = radii.tolist()
        allowed = eligible.tolist()
        for row in range(n):
            if not allowed[row]:
                continue
            cx, cy, cz = rows[row]
            ox, oy, oz = cx - px, cy - py, cz - pz
            distance_sq = ox * ox + oy * oy + oz * oz
            r = radius[row]
            if distance_sq >= r * r:
                continue
            forward_dot = ox * fx + oz * fz
            if forward_dot > 0.0 and forward_dot * forward_dot > 0.25 * distance_sq:
                return row
        return -1
    player_pos = np.asarray(pos, dtype=np.float32)
    offsets = centres - player_pos
    distance_sq = np.einsum("ij,ij->i", offsets, offsets)
    in_range = eligible & (distance_sq < radii * radii)
    if not in_range.any():
        return -1
    forward = np.asarray((fx, 0.0, fz), dtype=np.float32)
    forward_dot = offsets @ forward
    facing = (forward_dot > 0.0) & (forward_dot * forward_dot > (0.25 * distance_sq))
    candidates = np.flatnonzero(in_range & facing)
    return int(candidates[0]) if candidates.size else -1


class LogicInteraction:
    """Use-key interactions that are local to the player's world position."""

    def __init__(self, logic):
        self.logic = logic
        self.current_hud_message = ""
        self.current_hud_key_name = None
        self.level_complete_ui = None

    def handle(self, use_key_pressed: bool):
        logic = self.logic
        self.current_hud_message = ""
        self.current_hud_key_name = None

        reach_distance = 80.0
        px, py, pz = logic.player_runtime.player.pos

        found_door_idx = -1
        found_door_brush = None
        for i, brush in logic.mover_runtime.doors:
            pos = brush["pos"]
            size = brush["size"]
            dx = abs(pos[0] - px)
            dy = abs(pos[1] - py)
            dz = abs(pos[2] - pz)
            if (
                dx < size[0] / 2 + reach_distance
                and dz < size[2] / 2 + reach_distance
                and dy < size[1] / 2 + 64
            ):
                found_door_idx = i
                found_door_brush = brush
                break

        door_consumed_use = False
        if found_door_brush:
            door_state = logic.mover_runtime.door_states.get(
                found_door_idx, {}
            ).get("state", "closed")

            if door_state == "closed":
                if found_door_brush.get("door_auto_open", False):
                    is_locked = found_door_brush.get("door_locked", False)
                    needs_key = found_door_brush.get("door_needs_key", False)
                    if not is_locked and not needs_key:
                        logic.mover_runtime._trigger_door_open(
                            found_door_idx,
                            found_door_brush,
                        )
                else:
                    is_locked = found_door_brush.get("door_locked", False)
                    needs_key = found_door_brush.get("door_needs_key", False)
                    key_name = found_door_brush.get("door_key_name", "")

                    if is_locked:
                        self.current_hud_message = "Locked"
                        if use_key_pressed and logic.io_manager:
                            logic.io_manager.fire_output(
                                found_door_brush,
                                "OnLockedUse",
                            )
                        door_consumed_use = use_key_pressed
                    elif needs_key:
                        has_key = key_name in logic.player_runtime.collected_keys
                        if has_key:
                            self.current_hud_message = "[E] Use"
                            self.current_hud_key_name = key_name or None
                            if use_key_pressed:
                                logic.mover_runtime._trigger_door_open(
                                    found_door_idx,
                                    found_door_brush,
                                )
                                door_consumed_use = True
                        else:
                            self.current_hud_message = "Need"
                            self.current_hud_key_name = key_name or None
                    else:
                        self.current_hud_message = "[E] Open"
                        if use_key_pressed:
                            logic.mover_runtime._trigger_door_open(
                                found_door_idx,
                                found_door_brush,
                            )
                            door_consumed_use = True

        world = logic.world_runtime
        if not door_consumed_use and world.levelchanger_things:
            # Radius activation is squared, removing the old per-entry
            # distance sqrt. Facing is also tested without per-row
            # normalisation.
            if len(world.levelchanger_centres) != len(world.levelchanger_things):
                world.refresh_levelchanger_table()
            row = facing_levelchanger_row(
                world.levelchanger_centres, world.levelchanger_radii,
                world.levelchanger_eligible, (px, py, pz),
                logic.player_runtime.player.angle)
            if row >= 0:
                thing = world.levelchanger_things[row]
                self.current_hud_message = "[E] Complete Level"
                if use_key_pressed:
                    # The map and the PlayerStart there, as the
                    # LevelChanger's ChangeLevel input resolves them.
                    target_map, destination_spawn = thing.destination()
                    self.level_complete_ui = {
                        "active": True,
                        "target_map": target_map or "",
                        "destination_spawn": destination_spawn,
                        "title": "Complete",
                        "button_text": "Continue",
                    }
                    if thing.generates_level():
                        # Continue makes a new level rather than load one.
                        self.level_complete_ui.update(
                            target_map="",
                            destination_spawn="",
                            generate_level=True,
                            generator_params=thing.generator_params(),
                        )
                    if logic.io_manager:
                        logic.io_manager.fire_output(
                            thing,
                            "OnUse",
                        )
                return
