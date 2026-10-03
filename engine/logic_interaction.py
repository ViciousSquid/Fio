"""Player-to-world interaction handling delegated from LogicThread.

Owns the per-tick door/LevelChanger interaction query and prompt generation.
LogicThread retains the compatibility wrapper and authoritative tick ordering.
"""

from __future__ import annotations

import math

import numpy as np


class LogicInteraction:
    """Use-key interactions that are local to the player's world position."""

    def __init__(self, logic):
        self.logic = logic

    def handle(self, use_key_pressed: bool):
        logic = self.logic
        logic.current_hud_message = ""
        logic.current_hud_key_name = None

        reach_distance = 80.0
        px, py, pz = logic.player.pos

        found_door_idx = -1
        found_door_brush = None
        for i, brush in logic.doors:
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
            door_state = logic.door_states.get(
                found_door_idx, {}
            ).get("state", "closed")

            if door_state == "closed":
                if found_door_brush.get("door_auto_open", False):
                    is_locked = found_door_brush.get("door_locked", False)
                    needs_key = found_door_brush.get("door_needs_key", False)
                    if not is_locked and not needs_key:
                        logic._trigger_door_open(
                            found_door_idx,
                            found_door_brush,
                        )
                else:
                    is_locked = found_door_brush.get("door_locked", False)
                    needs_key = found_door_brush.get("door_needs_key", False)
                    key_name = found_door_brush.get("door_key_name", "")

                    if is_locked:
                        logic.current_hud_message = "Locked"
                        if use_key_pressed and logic.io_manager:
                            logic.io_manager.fire_output(
                                found_door_brush,
                                "OnLockedUse",
                            )
                        door_consumed_use = use_key_pressed
                    elif needs_key:
                        has_key = key_name in logic.collected_keys
                        if has_key:
                            logic.current_hud_message = "[E] Use"
                            logic.current_hud_key_name = key_name or None
                            if use_key_pressed:
                                logic._trigger_door_open(
                                    found_door_idx,
                                    found_door_brush,
                                )
                                door_consumed_use = True
                        else:
                            logic.current_hud_message = "Need"
                            logic.current_hud_key_name = key_name or None
                    else:
                        logic.current_hud_message = "[E] Open"
                        if use_key_pressed:
                            logic._trigger_door_open(
                                found_door_idx,
                                found_door_brush,
                            )
                            door_consumed_use = True

        if not door_consumed_use and logic._levelchanger_things:
            # Radius activation is squared, removing the old per-entry
            # distance sqrt. Facing is also tested without per-row
            # normalisation.
            centres = getattr(logic, "_levelchanger_centres", None)
            radii = getattr(logic, "_levelchanger_radii", None)
            eligible = getattr(logic, "_levelchanger_eligible", None)
            if (
                centres is None
                or radii is None
                or eligible is None
                or len(centres) != len(logic._levelchanger_things)
            ):
                logic._refresh_levelchanger_table()
                centres = logic._levelchanger_centres
                radii = logic._levelchanger_radii
                eligible = logic._levelchanger_eligible

            player_pos = np.asarray(
                (px, py, pz),
                dtype=np.float32,
            )
            offsets = centres - player_pos
            distance_sq = np.einsum("ij,ij->i", offsets, offsets)
            in_range = eligible & (distance_sq < radii * radii)

            if in_range.any():
                forward = np.asarray(
                    (
                        math.sin(logic.player.angle),
                        0.0,
                        math.cos(logic.player.angle),
                    ),
                    dtype=np.float32,
                )
                forward_dot = offsets @ forward
                facing = (
                    (forward_dot > 0.0)
                    & (forward_dot * forward_dot > (0.25 * distance_sq))
                )
                candidates = np.flatnonzero(in_range & facing)
                if candidates.size:
                    # Preserve authored list order: first matching row wins.
                    row = int(candidates[0])
                    thing = logic._levelchanger_things[row]
                    logic.current_hud_message = "[E] Complete Level"
                    if use_key_pressed:
                        target_map = thing.properties.get(
                            "target_map",
                            "",
                        )
                        logic.level_complete_ui = {
                            "active": True,
                            "target_map": target_map,
                            "title": "Complete",
                            "button_text": "Continue",
                        }
                        if logic.io_manager:
                            logic.io_manager.fire_output(
                                thing,
                                "OnUse",
                            )
                    return
