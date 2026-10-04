"""Portal runtime delegated from LogicThread.

Owns portal topology caching, portal fade/transit state, player transit and
projectile transit. LogicThread remains the tick-order orchestrator.
"""

from __future__ import annotations

import math

import glm
import numpy as np

from .portal_transform import (
    map_point as portal_map_point,
    map_direction as portal_map_direction,
)

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


_PORTAL_TRANSIT_COOLDOWN = 0.5
_PORTAL_PLAYER_EXIT_EPSILON = 0.05


class LogicPortals:
    """Runtime mechanics for world portals."""

    def __init__(self, logic, *, portal_type=None):
        self.logic = logic
        self.portal_type = portal_type
        self.portal_things = []
        self.portal_target_things = []
        self.portal_slots = np.empty(0, dtype=np.int32)
        self.portal_target_slots = np.empty(0, dtype=np.int32)

    def rebuild_links(self):
        """Resolve portal_target names to paired portal slots."""
        logic = self.logic
        Portal = self.portal_type

        self.portal_things = []
        self.portal_target_things = []
        portal_slots = []
        portal_target_slots = []

        name_to_slot = {
            t.properties.get("name"): slot
            for slot, t in enumerate(logic.editor_state.things)
            if t.properties.get("name")
        }

        for slot, thing in enumerate(logic.editor_state.things):
            if not (Portal and isinstance(thing, Portal)):
                continue

            self.portal_things.append(thing)
            portal_slots.append(slot)

            target_name = thing.properties.get("portal_target", "")
            target_slot = name_to_slot.get(target_name, -1)
            if (
                target_slot >= 0
                and Portal
                and isinstance(logic.editor_state.things[target_slot], Portal)
            ):
                portal_target_slots.append(target_slot)
                self.portal_target_things.append(logic.editor_state.things[target_slot])
            else:
                portal_target_slots.append(-1)
                self.portal_target_things.append(None)

        self.portal_slots = np.asarray(portal_slots, dtype=np.int32)
        self.portal_target_slots = np.asarray(
            portal_target_slots, dtype=np.int32
        )

    def reset_session(self):
        """Reset transit cooldowns and restore portal fade state."""
        logic = self.logic
        Portal = self.portal_type

        logic._portal_cooldowns.clear()
        logic._portal_prev_player_pos = None

        if Portal is not None:
            for thing in logic.editor_state.things:
                if isinstance(thing, Portal):
                    active = thing.is_active()
                    thing._fade_alpha = 1.0 if active else 0.0
                    thing._fade_target = thing._fade_alpha

    def note_player_teleported(self):
        """Invalidate the previous movement segment after a non-portal teleport."""
        self.logic._portal_prev_player_pos = None

    def update(self, delta: float):
        """Detect and execute player transit through active portal pairs."""
        logic = self.logic
        Portal = self.portal_type

        if Portal is None or not logic.player:
            return
        if not len(self._portal_things):
            return

        for portal in logic._portal_things:
            portal.tick_fade(delta)

        for pid in list(logic._portal_cooldowns):
            logic._portal_cooldowns[pid] -= delta
            if logic._portal_cooldowns[pid] <= 0.0:
                del logic._portal_cooldowns[pid]

        cur = (
            float(logic.player.pos.x),
            float(logic.player.pos.y),
            float(logic.player.pos.z),
        )
        prev = logic._portal_prev_player_pos
        if prev is None:
            prev = cur

        for portal_index, _portal_slot in enumerate(self._portal_slots):
            portal_a = logic._portal_things[portal_index]
            if not portal_a.is_active():
                continue
            if portal_index >= len(self._portal_target_slots):
                continue

            target_slot = int(logic._portal_target_slots[portal_index])
            if target_slot < 0:
                continue

            portal_b = self._portal_target_things[portal_index]
            if portal_b is None or not portal_b.is_active():
                continue
            if id(portal_a) in logic._portal_cooldowns:
                continue

            hit = self._segment_crosses_aperture(portal_a, prev, cur)
            if hit is None:
                continue

            self._execute_player_transit(portal_a, portal_b)

            cooldown = getattr(
                Portal, "TRANSIT_COOLDOWN", _PORTAL_TRANSIT_COOLDOWN
            )
            logic._portal_cooldowns[id(portal_a)] = cooldown
            logic._portal_cooldowns[id(portal_b)] = cooldown

            if logic.io_manager:
                logic.io_manager.fire_output(portal_a, "OnTeleport")
                logic.io_manager.fire_output(portal_a, "OnPlayerEnter")

            debug_log(
                "Portal",
                f"Player transited '{portal_a.properties.get('name')}' -> "
                f"'{portal_b.properties.get('name')}'",
            )
            break

        logic._portal_prev_player_pos = (
            float(logic.player.pos.x),
            float(logic.player.pos.y),
            float(logic.player.pos.z),
        )

    @staticmethod
    def _segment_crosses_aperture(portal, prev, cur):
        """Return the crossing point if prev->cur crosses front-to-back."""
        nx, ny, nz = portal.get_normal()
        ox, oy, oz = portal.pos
        s_prev = (
            (prev[0] - ox) * nx
            + (prev[1] - oy) * ny
            + (prev[2] - oz) * nz
        )
        s_cur = (
            (cur[0] - ox) * nx
            + (cur[1] - oy) * ny
            + (cur[2] - oz) * nz
        )

        if not (s_prev >= 0.0 and s_cur < 0.0):
            return None

        denom = s_prev - s_cur
        t = s_prev / denom if denom > 1e-9 else 0.0
        t = min(1.0, max(0.0, t))

        hit = (
            prev[0] + (cur[0] - prev[0]) * t,
            prev[1] + (cur[1] - prev[1]) * t,
            prev[2] + (cur[2] - prev[2]) * t,
        )
        if portal.contains_point(hit[0], hit[1], hit[2], margin=0.0):
            return hit
        return None

    def _execute_player_transit(self, portal_a, portal_b):
        """Teleport the player through a portal pair."""
        logic = self.logic
        player = logic.player
        if player is None:
            return

        p = player.pos
        tx, ty, tz = portal_map_point(
            portal_a.pos,
            portal_a.get_basis(),
            portal_b.pos,
            portal_b.get_basis(),
            (float(p.x), float(p.y), float(p.z)),
        )
        vx, vy, vz = portal_map_direction(
            portal_a.get_basis(),
            portal_b.get_basis(),
            (
                float(player.velocity.x),
                float(player.velocity.y),
                float(player.velocity.z),
            ),
        )

        bnx, bny, bnz = portal_b.get_normal()
        player.pos = glm.vec3(
            tx + bnx * _PORTAL_PLAYER_EXIT_EPSILON,
            ty + bny * _PORTAL_PLAYER_EXIT_EPSILON,
            tz + bnz * _PORTAL_PLAYER_EXIT_EPSILON,
        )
        player.velocity = glm.vec3(vx, vy, vz)

        angle = float(player.angle)
        pitch = float(getattr(player, "pitch", 0.0))
        fx = math.sin(angle) * math.cos(pitch)
        fy = math.sin(pitch)
        fz = math.cos(angle) * math.cos(pitch)
        mfx, mfy, mfz = portal_map_direction(
            portal_a.get_basis(),
            portal_b.get_basis(),
            (fx, fy, fz),
        )
        player.angle = math.atan2(mfx, mfz)
        if hasattr(player, "pitch"):
            player.pitch = math.asin(max(-1.0, min(1.0, mfy)))

        logic._plugin_emit(
            "portal_transit",
            portal_from=portal_a,
            portal_to=portal_b,
        )

    def transit_projectile_through_portals(
        self, projectiles, index, prev_pos
    ):
        """Teleport one dense projectile through cached portal relations."""
        logic = self.logic
        Portal = self.portal_type

        if Portal is None or not len(logic._portal_things):
            return

        cur = tuple(projectiles.pos[index])
        for portal_index, _portal_slot in enumerate(self._portal_slots):
            portal_a = logic._portal_things[portal_index]
            if not portal_a.is_active():
                continue
            if portal_index >= len(logic._portal_target_slots):
                continue

            target_slot = int(logic._portal_target_slots[portal_index])
            if target_slot < 0:
                continue

            portal_b = logic._portal_target_things[portal_index]
            if portal_b is None or not portal_b.is_active():
                continue

            if self._segment_crosses_aperture(portal_a, prev_pos, cur) is None:
                continue

            npx, npy, npz = portal_map_point(
                portal_a.pos,
                portal_a.get_basis(),
                portal_b.pos,
                portal_b.get_basis(),
                cur,
            )
            direction = portal_map_direction(
                portal_a.get_basis(),
                portal_b.get_basis(),
                tuple(projectiles.vel[index]),
            )
            bnx, bny, bnz = portal_b.get_normal()
            projectiles.pos[index] = (
                npx + bnx * Portal.EXIT_CLEARANCE,
                npy + bny * Portal.EXIT_CLEARANCE,
                npz + bnz * Portal.EXIT_CLEARANCE,
            )
            projectiles.vel[index] = direction
            break
