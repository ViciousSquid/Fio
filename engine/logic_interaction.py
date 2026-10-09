"""Player-to-world interaction handling delegated from LogicThread.

Owns the per-tick door/LevelChanger interaction query and prompt generation.
LogicThread remains the authoritative tick-order orchestrator.
"""

from __future__ import annotations

import math

import numpy as np


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
            centres = world.levelchanger_centres
            radii = world.levelchanger_radii
            eligible = world.levelchanger_eligible

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
                        math.sin(logic.player_runtime.player.angle),
                        0.0,
                        math.cos(logic.player_runtime.player.angle),
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
