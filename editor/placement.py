"""Where something added from a menu or the Asset Browser goes.

New models used to appear at the world origin, wherever the user was. They
now go where the user is: just ahead of the editor camera (along its heading,
so the new object is in view rather than underneath the camera), stopped
short of the first wall in the way, and set down on the floor -- the highest
solid brush top, or the terrain, below that point.

Qt-free: the editor passes the camera and the scene in.
"""

from __future__ import annotations

import math

from engine.brush_placement import ray_box_entry, world_box
from engine.constants import is_solid_world_brush

#: How far ahead of the camera a new object is placed, in world units.
PLACE_AHEAD = 160.0
#: How far short of a wall in the way it stops.
WALL_MARGIN = 24.0


def _solid_boxes(brushes):
    for brush in brushes or ():
        try:
            if is_solid_world_brush(brush):
                yield world_box(brush)
        except Exception:
            continue                    # a malformed brush is not a floor


def floor_below(x, z, top_y, brushes, terrain=None):
    """The highest solid surface at (x, z) no higher than *top_y*, or None."""
    best = None
    for cx, cy, cz, hx, hy, hz in _solid_boxes(brushes):
        if abs(x - cx) <= hx and abs(z - cz) <= hz:
            top = cy + hy
            if top <= top_y and (best is None or top > best):
                best = top
    if terrain is not None:
        try:
            ground = terrain.get_height_at_safe(x, z)
        except Exception:
            ground = None
        if ground is not None and ground <= top_y and (best is None or ground > best):
            best = float(ground)
    return best


def placement_ahead(camera_pos, yaw_degrees, brushes, terrain=None,
                    distance=PLACE_AHEAD, grid=0):
    """``[x, y, z]`` for a new object placed in front of the camera.

    *yaw_degrees* is the editor camera's yaw (its front vector is
    ``(cos yaw, ., sin yaw)``). The point is *distance* ahead on the
    horizontal, pulled back to stop :data:`WALL_MARGIN` short of the first
    solid brush the camera looks through, snapped to *grid* (0: no snap) and
    dropped onto the floor below. With no floor under it, it stays at the
    camera's height.
    """
    ox, oy, oz = float(camera_pos[0]), float(camera_pos[1]), float(camera_pos[2])
    yaw = math.radians(float(yaw_degrees))
    dx, dz = math.cos(yaw), math.sin(yaw)
    reach = float(distance)
    origin, direction = (ox, oy, oz), (dx, 0.0, dz)
    for box in _solid_boxes(brushes):
        t = ray_box_entry(box, origin, direction)
        if t is not None and t < reach + WALL_MARGIN:
            reach = max(0.0, t - WALL_MARGIN)
    x, z = ox + dx * reach, oz + dz * reach
    if grid and grid > 0:
        x = round(x / grid) * grid
        z = round(z / grid) * grid
    floor = floor_below(x, z, oy, brushes, terrain)
    y = oy if floor is None else floor
    return [float(x), float(y), float(z)]
