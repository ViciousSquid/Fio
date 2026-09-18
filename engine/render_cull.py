"""
Camera render-distance cull -- the pure, GL-free geometry of it.

The renderer's main camera pass runs this cheap broad-phase cull *before*
``_sort_objects`` (and on top of the frustum cull it already does): any object
whose centre lies farther than the camera's view distance on the XZ plane is
dropped. Distances are compared squared, so no square root runs per object.

The radius itself is *not* here any more. It is a live camera setting the
editor and the console can move mid-session, so it lives on
:class:`engine.view_distance.ViewDistance` and the renderer reads it per frame;
:data:`CAMERA_RENDER_CULL_DISTANCE` below is only that setting's default value,
kept under its old name for callers and tests that want the number.

The logic lives here, apart from :mod:`engine.renderer_F`, for two reasons: it
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

#: Default outer limit (world units) on the XZ plane, measured from the camera
#: centre. A *ceiling*, not the working radius: :func:`visible_xz_bounds`
#: derives the actual relevant region from the live camera, which for a
#: steeply-angled or top-down view is several times tighter. The ceiling still
#: matters -- it is what bounds a first-person view whose frustum runs all the
#: way to the far plane.
#:
#: Aliased from :mod:`engine.view_distance` so the default draw distance is
#: written down once; a running camera's actual radius is read from its
#: ViewDistance, not from here.
CAMERA_RENDER_CULL_DISTANCE = DEFAULT_VIEW_DISTANCE
#: Precomputed squared radius -- the value the per-object test actually compares.
CAMERA_RENDER_CULL_DISTANCE_SQ = CAMERA_RENDER_CULL_DISTANCE * CAMERA_RENDER_CULL_DISTANCE

#: How far above and below the world's geometry the visible slab is extended, so
#: a tall billboard, a floating light or a jumping actor at the very top or
#: bottom of the world is never clipped out of the relevant region.
WORLD_SLAB_MARGIN = 512.0


def visible_xz_bounds(cam, corners, y_min, y_max,
                      max_dist=CAMERA_RENDER_CULL_DISTANCE):
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
    four far-plane corner points in world space; each is clipped as a segment
    from *cam*, first to *max_dist* and then to the slab ``[y_min, y_max]`` the
    world's geometry occupies.  The XZ bounds of what survives is the answer,
    plus the camera's own XZ when it sits inside the slab, so nothing directly
    beneath a first-person camera is ever dropped.

    Both modes are served by the same arithmetic, and the degenerate one is the
    safe one: at low pitch the corner rays stay inside the slab for their whole
    length, the slab clip does nothing, and the result collapses to the
    far-plane footprint bounded by *max_dist* -- the distance ceiling Fio culled
    by before.  So this can be called unconditionally; it tightens the overhead
    case and reproduces the old behaviour in first person.  It is stateless, so
    a mid-play mode switch cannot leave it stale -- but a *caller* that caches
    the box against the player's position can, because the box changes on a
    switch while the player has not moved.

    This is a *visibility* answer, not a residency one (§15).  Big World decides
    what is resident, from the **player's** position; this decides what of that
    is drawn, from the **camera's**.  Keeping those two inputs separate is what
    stops a camera toggle from streaming cells in or out: in first person the
    visible box can reach past the activation radius, and it must not drag the
    world's resident set out with it.  Narrowing this box never unloads anything
    and never suppresses simulation.

    Returns ``(min_x, min_z, max_x, max_z)``.  Conservative by construction: the
    bounding box of the visible volume, never smaller than it.

    Pure arithmetic -- no glm, no GL -- so it is unit-testable headlessly.
    """
    cx, cy, cz = float(cam[0]), float(cam[1]), float(cam[2])
    lo_y = min(y_min, y_max) - WORLD_SLAB_MARGIN
    hi_y = max(y_min, y_max) + WORLD_SLAB_MARGIN

    min_x = max_x = cx
    min_z = max_z = cz
    if not (lo_y <= cy <= hi_y):
        min_x = min_z = float("inf")
        max_x = max_z = float("-inf")

    # The corner rays bound the *lateral* extent, but not the forward one: they
    # leave the eye at the frustum's widest angle, so they are much longer than
    # the view axis and clipping them to `max_dist` stops short of it.  At a 75
    # degree FOV that lands the box a little over half way to the ceiling, which
    # would wrongly cull something dead ahead -- invisible in an overhead view,
    # where the slab clip dominates long before the ceiling does, and plainly
    # wrong in first person.  The axis ray is what reaches furthest forward, and
    # for a symmetric frustum it passes through the centroid of the four far
    # corners, so sampling that alongside them bounds both extents.
    samples = list(corners)
    if len(samples) >= 3:
        inv_n = 1.0 / len(samples)
        samples.append((sum(float(c[0]) for c in samples) * inv_n,
                        sum(float(c[1]) for c in samples) * inv_n,
                        sum(float(c[2]) for c in samples) * inv_n))

    for corner in samples:
        dx = float(corner[0]) - cx
        dy = float(corner[1]) - cy
        dz = float(corner[2]) - cz
        # Clip the ray's length to the hard ceiling first.
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        t_far = 1.0 if length <= max_dist or length == 0.0 else max_dist / length
        t0, t1 = 0.0, t_far
        # Then to the world's height slab.
        if abs(dy) < 1e-9:
            if not (lo_y <= cy <= hi_y):
                continue                      # parallel to the slab and outside it
        else:
            ta = (lo_y - cy) / dy
            tb = (hi_y - cy) / dy
            if ta > tb:
                ta, tb = tb, ta
            t0 = max(t0, ta)
            t1 = min(t1, tb)
            if t0 > t1:
                continue                      # the segment never enters the slab
        for t in (t0, t1):
            x = cx + dx * t
            z = cz + dz * t
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if z < min_z:
                min_z = z
            if z > max_z:
                max_z = z

    if min_x > max_x:
        # Nothing in the slab is visible at all: degenerate to the camera point
        # rather than returning an inverted box a caller would misread.
        return (cx, cz, cx, cz)
    return (min_x, min_z, max_x, max_z)


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
                     limit_sq: float = CAMERA_RENDER_CULL_DISTANCE_SQ,
                     out: Optional[List] = None,
                     keep: Optional[Callable[[object], bool]] = None,
                     positions=None) -> List:
    """Return the subset of objects within limit_sq XZ of (cx, cz).

    Fills and returns out when given (cleared first), so a caller can reuse one
    persistent buffer across frames and allocate nothing; otherwise a fresh list
    is returned. An object for which keep returns True -- or that has no
    readable position -- is retained unconditionally (fail-open: never wrongly
    hide it).

    positions is an optional contiguous (N, 2) NumPy array of [x, z] rows
    aligned one-for-one with objects. When supplied, the X/Z distance arithmetic
    is evaluated in one NumPy batch; the Python object walk is then limited to
    assembling the surviving objects into out. Existing callers that do not
    provide it keep the original scalar path and behaviour.

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

        # The expensive scalar kernel is the part worth moving out of Python.
        # No object references are copied into NumPy; only numeric X/Z rows are
        # touched here, and the API's Python list is rebuilt from the mask below.
        dx = positions[:, 0] - cx
        dz = positions[:, 1] - cz
        visible = (dx * dx + dz * dz) <= limit_sq

        if keep is not None:
            # keep remains an object-level callback because its semantics are
            # arbitrary. In Fio it is a cheap isinstance check; the expensive
            # distance arithmetic above is still fully batched.
            forced = np.fromiter(
                (bool(keep(obj)) for obj in objects),
                dtype=bool,
                count=count,
            )
            visible |= forced

        # Avoid a Python walk over every culled object. The public API still has
        # to return actual Python objects, so dense visible sets necessarily do
        # O(number of returned objects) work here.
        for index in np.flatnonzero(visible):
            out.append(objects[int(index)])
        return out

    # Original API-compatible scalar path.
    for obj in objects:
        if keep is not None and keep(obj):
            out.append(obj)
            continue
        pos = pos_of(obj)
        if pos is None or within_xz_sq(pos, cx, cz, limit_sq):
            out.append(obj)
    return out
