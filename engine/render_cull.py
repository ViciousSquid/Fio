"""
Camera render-distance cull -- the pure, GL-free geometry of it.

The renderer's main camera pass runs this cheap broad-phase cull *before*
``_sort_objects`` (and on top of the frustum cull it already does): any object
whose centre lies farther than the camera's view distance on the XZ plane is
dropped. Distances are compared squared, so no square root runs per object.

The radius itself is *not* here any more. It is a live camera setting the
editor and the console can move mid-session, so it lives on
:class:`engine.view_distance.ViewDistance` and the renderer reads it per frame;

The logic lives here, apart from :mod:`engine.renderer`, for two reasons: it
carries no OpenGL/glm/Qt dependency, so it is unit-testable headlessly; and it
keeps the renderer's per-frame path a thin call over a persistent scratch buffer
(no per-frame list allocation). The shadow and portal passes deliberately do not
call this -- they keep operating on the full scene.
"""

from __future__ import annotations

import math

import numpy as np
from typing import Callable, List, Optional, Sequence

from engine.view_distance import DEFAULT_VIEW_DISTANCE

#: How far above and below the world's geometry the visible slab is extended, so
#: a tall billboard, a floating light or a jumping actor at the very top or
#: bottom of the world is never clipped out of the relevant region.
WORLD_SLAB_MARGIN = 512.0


def visible_xz_bounds(cam, corners, y_min, y_max,
                      max_dist=DEFAULT_VIEW_DISTANCE):
    """The XZ box the camera can actually see, given the world's height slab.

    Fio has two play cameras and the player switches between them mid-session, so
    one fixed radius cannot serve both.  Overhead floats ``overhead_height``
    above the player raked by ``overhead_tilt``: its frustum leaves the world's
    vertical slab almost immediately, and the ground it covers is a box a couple
    of thousand units across rather than the tens of thousands a far plane at
    10,000 would suggest.  First person sits at the player and looks out to the
    ceiling.  A radius wide enough for the second draws a ring of world nobody
    can see in the first.

    So the region is derived from the live camera instead.  *corners* are the
    four far-plane corner points in world space; with *cam* they span the view
    pyramid.  That pyramid is cut down to what lies within *max_dist* of the
    camera, then clipped to the slab ``[y_min, y_max]`` the world's geometry
    occupies, and the answer is the XZ bounds of what survives.

    The clip is exact rather than sampled.  A convex solid cut by two parallel
    planes has as vertices only its own vertices inside the slab and the points
    where its edges cross the planes, so every segment between two of the five
    points is clipped and its surviving ends collected.  Sampling only the
    corner rays is not enough: in a level first-person view over a thin world
    they leave the slab within a few hundred units, while the far face still
    crosses it thousands of units to either side.

    Both modes are served by the same arithmetic.  At low pitch the pyramid
    stays in the slab and the result is its footprint out to *max_dist*; at a
    steep pitch the slab clip dominates and the box shrinks to the patch of
    ground below.  It is stateless, so a mid-play mode switch cannot leave it
    stale -- but a *caller* that caches the box against the player's position
    can, because the box changes on a switch while the player has not moved.

    This is a *visibility* answer, not a residency one (§15).  Big World decides
    what is resident, from the **player's** position; this decides what of that
    is drawn, from the **camera's**.  Keeping those two inputs separate is what
    stops a camera toggle from streaming cells in or out: in first person the
    visible box can reach past the activation radius, and it must not drag the
    world's resident set out with it.  Narrowing this box never unloads anything
    and never suppresses simulation.

    Returns ``(min_x, min_z, max_x, max_z)``.  Conservative by construction: the
    bounding box of the visible volume, never smaller than it.  (It bounds the
    volume, not what an AABB-against-planes test accepts: that test also passes
    some boxes near the frustum's edges that lie wholly outside it.)

    Pure arithmetic -- no glm, no GL -- so it is unit-testable headlessly.
    """
    cx, cy, cz = float(cam[0]), float(cam[1]), float(cam[2])
    lo_y = min(y_min, y_max) - WORLD_SLAB_MARGIN
    hi_y = max(y_min, y_max) + WORLD_SLAB_MARGIN
    offsets = [(float(c[0]) - cx, float(c[1]) - cy, float(c[2]) - cz)
               for c in corners]

    # Everything within max_dist of the camera lies no deeper than max_dist
    # below it along the far plane's normal, so the pyramid cut at that depth
    # -- the far corners pulled in by depth / max_dist -- contains it.
    scale = 1.0
    if len(offsets) >= 3:
        (ax, ay, az), (bx, by, bz), (qx, qy, qz) = offsets[:3]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = qx - ax, qy - ay, qz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        n_len = math.sqrt(nx * nx + ny * ny + nz * nz)
        if n_len > 0.0:
            depth = abs(ax * nx + ay * ny + az * nz) / n_len
            if depth > max_dist:
                scale = max_dist / depth
    points = [(0.0, 0.0, 0.0)] + [(x * scale, y * scale, z * scale)
                                  for x, y, z in offsets]

    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    dy_lo, dy_hi = lo_y - cy, hi_y - cy
    for i, (px, py, pz) in enumerate(points):
        if dy_lo <= py <= dy_hi:
            min_x, max_x = min(min_x, px), max(max_x, px)
            min_z, max_z = min(min_z, pz), max(max_z, pz)
        for qx, qy, qz in points[i + 1:]:
            span = qy - py
            if span == 0.0:
                continue
            for plane_y in (dy_lo, dy_hi):
                t = (plane_y - py) / span
                if 0.0 < t < 1.0:
                    x = px + (qx - px) * t
                    z = pz + (qz - pz) * t
                    min_x, max_x = min(min_x, x), max(max_x, x)
                    min_z, max_z = min(min_z, z), max(max_z, z)

    if min_x > max_x:
        # Nothing in the slab is visible at all: degenerate to the camera point
        # rather than returning an inverted box a caller would misread.
        return (cx, cz, cx, cz)
    # The cut pyramid overshoots max_dist towards its corners; the ball is
    # what was asked for, and its own bounding box trims that back.
    return (cx + max(min_x, -max_dist), cz + max(min_z, -max_dist),
            cx + min(max_x, max_dist), cz + min(max_z, max_dist))


def pos_of(obj):
    """The ``[x, y, z]`` of a brush dict or a Thing-like object, or ``None``."""
    if isinstance(obj, dict):
        return obj.get("pos")
    return getattr(obj, "pos", None)


def within_xz_sq(pos, cx: float, cz: float, limit_sq: float) -> bool:
    """Whether *pos* is within a squared XZ distance *limit_sq* of (cx, cz)."""
    dx = pos[0] - cx
    dz = pos[2] - cz
    return dx * dx + dz * dz <= limit_sq


def camera_xz(camera_pos):
    """(x, z) of the camera centre, accepting a glm vec or any ``[x, y, z]``."""
    if hasattr(camera_pos, "x"):
        return float(camera_pos.x), float(camera_pos.z)
    return float(camera_pos[0]), float(camera_pos[2])


def cull_by_distance(objects: Sequence, cx: float, cz: float,
                     limit_sq: float = DEFAULT_VIEW_DISTANCE * DEFAULT_VIEW_DISTANCE,
                     out: Optional[List] = None,
                     keep: Optional[Callable[[object], bool]] = None,
                     positions=None, positions_out=None) -> List:
    """Return the subset of objects within limit_sq XZ of (cx, cz).

    Fills and returns out when given (cleared first), so a caller can reuse one
    persistent buffer across frames and allocate nothing; otherwise a fresh list
    is returned. An object for which keep returns True -- or that has no
    readable position -- is retained unconditionally (fail-open: never wrongly
    hide it).

    positions is an optional contiguous (N, 2) NumPy array of [x, z] rows
    aligned one-for-one with objects. When supplied, the X/Z distance arithmetic
    is evaluated in one NumPy batch; the Python object walk is then limited to
    assembling the surviving objects into out. If positions_out is supplied,
    the selected rows are copied into that reusable buffer in the same batch,
    keeping a numeric position array aligned with the returned object list for
    subsequent vectorized stages.

    The batch contract is explicit: every object must have a row in positions.
    The engine uses it only with render-state snapshots built from authoritative
    Thing.pos values, so missing-position fail-open behaviour remains unchanged
    for the legacy/API-compatible path.
    """
    if out is None:
        out = []
    else:
        del out[:]

    if positions is not None:
        count = len(objects)
        if len(positions) != count:
            raise ValueError(
                "distance-cull positions must contain one [x, z] row per object "
                "(got %d rows for %d objects)" % (len(positions), count)
            )
        positions = np.asarray(positions)
        if positions.ndim != 2 or positions.shape[1] != 2:
            raise ValueError("distance-cull positions must have shape (N, 2), got %r" %
                             (positions.shape,))

        dx = positions[:, 0] - cx
        dz = positions[:, 1] - cz
        visible = (dx * dx + dz * dz) <= limit_sq

        if keep is not None:
            forced = np.fromiter(
                (bool(keep(obj)) for obj in objects),
                dtype=bool,
                count=count,
            )
            visible |= forced

        visible_indices = np.flatnonzero(visible)
        if positions_out is not None:
            if len(positions_out) < len(visible_indices):
                raise ValueError(
                    "distance-cull positions_out is too small "
                    "(got %d rows for %d visible objects)" %
                    (len(positions_out), len(visible_indices))
                )
            np.take(positions[:, 0], visible_indices,
                    out=positions_out[:len(visible_indices), 0])
            np.take(positions[:, 1], visible_indices,
                    out=positions_out[:len(visible_indices), 1])
        for index in visible_indices:
            out.append(objects[int(index)])
        return out

    for obj in objects:
        if keep is not None and keep(obj):
            out.append(obj)
            continue
        pos = pos_of(obj)
        if pos is None or within_xz_sq(pos, cx, cz, limit_sq):
            out.append(obj)
    return out


def sort_by_distance(objects: Sequence, positions, cx: float, cz: float,
                     reverse: bool = True, min_numpy_count: int = 16) -> List:
    """Return *objects* depth-sorted using batched NumPy distance math."""
    count = len(objects)
    if count < 2:
        return list(objects)

    pos = None if positions is None else np.asarray(positions)
    if (positions is None or count < min_numpy_count or
            pos is None or pos.dtype == object):
        result = list(objects)

        def _key(obj):
            pos = pos_of(obj)
            if pos is None:
                return float("inf") if reverse else float("-inf")
            dx = pos[0] - cx
            dz = pos[2] - cz
            value = dx * dx + dz * dz
            return -value if reverse else value

        result.sort(key=_key)
        return result

    if pos.ndim != 2 or pos.shape[0] != count or pos.shape[1] != 2:
        raise ValueError(
            "distance-sort positions must have shape (%d, 2), got %r" %
            (count, pos.shape)
        )
    dx = pos[:, 0] - cx
    dz = pos[:, 1] - cz
    distances = dx * dx + dz * dz
    order = np.argsort(-distances if reverse else distances, kind="stable")
    return [objects[int(index)] for index in order]
