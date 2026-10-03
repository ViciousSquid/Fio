"""Parented light and portal runtime delegated from LogicThread.

This subsystem owns the relationship between mover brushes and entities that
follow them. LogicThread remains responsible for session ordering and calls the
runtime at play-mode start, stop, and once per simulation tick.
"""

from __future__ import annotations

import math


class LogicParenting:
    """Runtime for lights and portals parented to mover brushes."""

    def __init__(self, logic, *, light_type=None, portal_type=None):
        self.logic = logic
        self.light_type = light_type
        self.portal_type = portal_type

    def _init_parented_lights(self):
        logic = self.logic
        logic._parented_lights = []
        Light = self.light_type
        if not Light:
            return

        for thing in logic.things:
            if not isinstance(thing, Light):
                continue

            parent_name = thing.properties.get("parent_mover", "")
            if not parent_name:
                continue

            brush = None
            for candidate in logic.brushes:
                if candidate.get("is_mover") and candidate.get("name") == parent_name:
                    brush = candidate
                    break

            if brush is None:
                print(
                    f"[Light] Warning: parent_mover '{parent_name}' not found "
                    f"for light '{thing.name}'"
                )
                continue

            thing.properties["_original_pos"] = list(thing.pos)
            offset = thing.properties.get("parent_offset")
            if not offset or offset == [0.0, 0.0, 0.0]:
                offset = [
                    thing.pos[0] - brush["pos"][0],
                    thing.pos[1] - brush["pos"][1],
                    thing.pos[2] - brush["pos"][2],
                ]
                thing.properties["parent_offset"] = offset

            logic._parented_lights.append((thing, brush, offset))

    def _reset_parented_lights(self):
        logic = self.logic
        for light, _brush, _offset in logic._parented_lights:
            original = light.properties.pop("_original_pos", None)
            if original is not None:
                light.pos = list(original)
        logic._parented_lights = []

    def _update_parented_lights(self):
        logic = self.logic
        for light, brush, offset in logic._parented_lights:
            bpos = brush["pos"]
            light.pos = [
                bpos[0] + offset[0],
                bpos[1] + offset[1],
                bpos[2] + offset[2],
            ]

    def _init_parented_portals(self):
        logic = self.logic
        logic._parented_portals = []
        Portal = self.portal_type
        if Portal is None:
            return

        for thing in logic.things:
            if not isinstance(thing, Portal):
                continue

            parent_name = thing.properties.get("parent_mover", "")
            if not parent_name:
                continue

            brush = None
            for candidate in logic.brushes:
                if candidate.get("is_mover") and candidate.get("name") == parent_name:
                    brush = candidate
                    break

            if brush is None:
                print(
                    f"[Portal] Warning: parent_mover '{parent_name}' not found "
                    f"for portal '{thing.properties.get('name', '')}'"
                )
                continue

            thing.properties["_original_pos"] = list(thing.pos)
            thing.properties["_original_yaw"] = thing.get_yaw_degrees()

            if thing.properties.get("parent_local_pos") is None:
                mover_yaw = brush.get("rotation_yaw", 0.0)
                thing.set_parent_local_transform(brush["pos"], mover_yaw)

            local_pos = thing.get_parent_local_pos()
            local_yaw = thing.get_parent_local_yaw()

            logic._parented_portals.append(
                (thing, brush, local_pos, local_yaw)
            )

    def _reset_parented_portals(self):
        logic = self.logic
        for portal, _brush, _local_pos, _local_yaw in logic._parented_portals:
            original = portal.properties.pop("_original_pos", None)
            if original is not None:
                portal.pos = list(original)

            original_yaw = portal.properties.pop("_original_yaw", None)
            if original_yaw is not None:
                portal.set_yaw_degrees(original_yaw)

        logic._parented_portals = []

    def _update_parented_portals(self):
        logic = self.logic
        for portal, brush, local_pos, local_yaw in logic._parented_portals:
            mover_pos = brush["pos"]
            mover_yaw = brush.get("rotation_yaw", 0.0)

            yaw_rad = math.radians(mover_yaw)
            cos_y = math.cos(yaw_rad)
            sin_y = math.sin(yaw_rad)

            world_x = (
                mover_pos[0]
                + local_pos[0] * cos_y
                - local_pos[2] * sin_y
            )
            world_z = (
                mover_pos[2]
                + local_pos[0] * sin_y
                + local_pos[2] * cos_y
            )
            portal.pos = [
                world_x,
                mover_pos[1] + local_pos[1],
                world_z,
            ]

            portal.set_yaw_degrees(mover_yaw + local_yaw)
