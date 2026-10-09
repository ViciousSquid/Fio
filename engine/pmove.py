"""Box traces for Quake 2 style player movement.

Quake 2 moves the player by *tracing*: it sweeps the player's box from where it
is to where its velocity would take it and stops at the first solid surface,
reporting how far it got and the plane it hit. Everything in
:class:`engine.player.Player` -- sliding along walls, stepping up stairs,
deciding what counts as ground -- is built on that one query, so this module is
the whole collision model.

The sweep is the usual Minkowski formulation: rather than moving a box through
convex solids, each solid's face planes are pushed out by the box's extent
along their normal and a single point is moved through the enlarged solid. A
point is inside a convex solid when it is behind every one of its planes, so
the segment's entry into the solid is the latest plane it crosses on the way in
and its exit is the earliest one it crosses on the way out.

Colliders, all given in Fio's +Y-up world space:

* box brushes -- their six axis-aligned faces;
* angled (convex) brushes -- their face planes plus the six faces of their
  bounding box. The extra axial planes are redundant for the solid itself but
  trim the expanded shape at its edges and corners, where pushing out only the
  face planes would make the solid catch a box that is not touching it;
* model meshes without planes -- their bounding box, as Quake 2 treats every
  entity it has no brush model for;
* terrain -- a heightfield the box's bottom centre may not go below.

A trace that starts inside a solid but leaves it is let through (it reports
``startsolid``); one that starts and ends inside reports ``allsolid`` and does
not move.
"""

from __future__ import annotations

import math

from .constants import PM_DIST_EPSILON, brush_aabb_bounds


class Trace:
    """The result of one box sweep."""

    __slots__ = ('fraction', 'endpos', 'normal', 'ent', 'startsolid', 'allsolid')

    def __init__(self, start):
        self.fraction = 1.0
        self.endpos = start
        self.normal = (0.0, 0.0, 0.0)   # the plane hit; zero when nothing was
        self.ent = None                 # the brush (or terrain) hit
        self.startsolid = False
        self.allsolid = False


def _axial_planes(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z):
    """The six faces of an axis-aligned box as outward ``(nx, ny, nz, d)``."""
    return ((1.0, 0.0, 0.0, hi_x), (-1.0, 0.0, 0.0, -lo_x),
            (0.0, 1.0, 0.0, hi_y), (0.0, -1.0, 0.0, -lo_y),
            (0.0, 0.0, 1.0, hi_z), (0.0, 0.0, -1.0, -lo_z))


def collider_from_brush(brush):
    """``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z, planes, brush)`` or None.

    Planes are outward-facing: a point *p* is inside when
    ``dot(n, p) <= d`` for every one of them.
    """
    if brush.get('_collision_mode') == 'mesh':
        bounds = brush.get('_mesh_bounds')
        if not bounds:
            return None
        (lo_x, lo_y, lo_z), (hi_x, hi_y, hi_z) = bounds
        lo = (float(lo_x), float(lo_y), float(lo_z))
        hi = (float(hi_x), float(hi_y), float(hi_z))
        planes = _axial_planes(*lo, *hi)
        faces = brush.get('_mesh_planes')
        if faces:
            planes = tuple(faces) + planes
        return (*lo, *hi, planes, brush)
    b = brush_aabb_bounds(brush)
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = (float(v) for v in b)
    return (lo_x, lo_y, lo_z, hi_x, hi_y, hi_z,
            _axial_planes(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z), brush)


def _clip_to_solid(sx, sy, sz, ex, ey, ez, hx, hy, hz, planes, trace, ent):
    """Clip the sweep against one convex solid, updating *trace* in place."""
    enter = -1.0
    leave = 1.0
    hit_normal = None
    starts_out = False
    ends_out = False
    for nx, ny, nz, d in planes:
        # Push the plane out by the box's extent along its normal.
        d += abs(nx) * hx + abs(ny) * hy + abs(nz) * hz
        d1 = nx * sx + ny * sy + nz * sz - d
        d2 = nx * ex + ny * ey + nz * ez - d
        if d2 > 0.0:
            ends_out = True
        if d1 > 0.0:
            starts_out = True
            if d2 >= d1:
                return              # outside this face for the whole sweep
        elif d2 <= 0.0:
            continue                # behind this face for the whole sweep
        if d1 > d2:                 # crossing in: stop short of the face
            f = (d1 - PM_DIST_EPSILON) / (d1 - d2)
            if f > enter:
                enter = f
                hit_normal = (nx, ny, nz)
        else:                       # crossing out
            f = (d1 + PM_DIST_EPSILON) / (d1 - d2)
            if f < leave:
                leave = f
    if not starts_out:
        trace.startsolid = True
        if not ends_out:
            trace.allsolid = True
            trace.ent = ent
        return
    if enter < leave and -1.0 < enter < trace.fraction:
        trace.fraction = max(enter, 0.0)
        trace.normal = hit_normal
        trace.ent = ent


# Terrain is a heightfield, not a convex solid: the sweep is sampled along its
# length and the first sample below the surface is refined by bisection.
_TERRAIN_SAMPLE_SPACING = 8.0
_TERRAIN_MAX_SAMPLES = 32
_TERRAIN_REFINE_STEPS = 8
_TERRAIN_NORMAL_STEP = 2.0
# Floating-point slack before a box resting on terrain counts as inside it.
_TERRAIN_SOLID_TOLERANCE = 0.01


def terrain_normal(terrain, x, z):
    """The terrain's upward surface normal at (x, z), by central differences."""
    h = _TERRAIN_NORMAL_STEP
    hx0 = terrain.get_height_at_safe(x - h, z)
    hx1 = terrain.get_height_at_safe(x + h, z)
    hz0 = terrain.get_height_at_safe(x, z - h)
    hz1 = terrain.get_height_at_safe(x, z + h)
    if None in (hx0, hx1, hz0, hz1):
        return (0.0, 1.0, 0.0)
    gx = (hx1 - hx0) / (2.0 * h)
    gz = (hz1 - hz0) / (2.0 * h)
    inv = 1.0 / math.sqrt(gx * gx + 1.0 + gz * gz)
    return (-gx * inv, inv, -gz * inv)


def _clip_to_terrain(sx, sy, sz, ex, ey, ez, hy, terrain, trace):
    """Clip the sweep against the terrain under the box's bottom centre."""
    def clearance(t):
        x = sx + (ex - sx) * t
        z = sz + (ez - sz) * t
        h = terrain.get_height_at_safe(x, z)
        if h is None:
            return math.inf
        return (sy + (ey - sy) * t - hy) - h

    start_clearance = clearance(0.0)
    if start_clearance < -_TERRAIN_SOLID_TOLERANCE:
        trace.startsolid = True
        if clearance(1.0) < -_TERRAIN_SOLID_TOLERANCE:
            trace.allsolid = True
            trace.ent = terrain
        return
    if sx == ex and sy == ey and sz == ez:
        return                      # a position test only asks "inside?"

    # A box resting a hair below the surface (rounding) may move, as long as
    # it goes no deeper than it started.
    floor = min(0.0, start_clearance)
    span = math.hypot(ex - sx, ez - sz)
    samples = min(_TERRAIN_MAX_SAMPLES,
                  max(1, int(math.ceil(span / _TERRAIN_SAMPLE_SPACING))))
    limit = trace.fraction
    lo = 0.0
    hit = None
    for i in range(1, samples + 1):
        t = i / samples
        if t > limit:
            t = limit
        if clearance(t) < floor:
            hit = t
            break
        lo = t
        if t >= limit:
            return
    if hit is None:
        return
    for _ in range(_TERRAIN_REFINE_STEPS):
        mid = (lo + hit) * 0.5
        if clearance(mid) < floor:
            hit = mid
        else:
            lo = mid
    if lo < trace.fraction:
        trace.fraction = lo
        x = sx + (ex - sx) * lo
        z = sz + (ez - sz) * lo
        trace.normal = terrain_normal(terrain, x, z)
        trace.ent = terrain


class BoxTracer:
    """Every solid the player can touch this frame, ready to be swept against."""

    __slots__ = ('colliders', 'terrain')

    def __init__(self, brushes, terrain=None):
        colliders = []
        for brush in brushes:
            c = collider_from_brush(brush)
            if c is not None:
                colliders.append(c)
        self.colliders = colliders
        self.terrain = terrain if terrain is not None and terrain.is_solid() else None

    def trace(self, start, end, half):
        """Sweep a box of half-extents *half* from *start* to *end*.

        *start*, *end* and *half* are ``(x, y, z)`` sequences; the box is
        centred on the swept point.
        """
        sx, sy, sz = (float(v) for v in start)
        ex, ey, ez = (float(v) for v in end)
        hx, hy, hz = (float(v) for v in half)
        trace = Trace((sx, sy, sz))

        # Broad phase: the box swept from start to end, plus a margin.
        m = 1.0
        min_x = min(sx, ex) - hx - m
        max_x = max(sx, ex) + hx + m
        min_y = min(sy, ey) - hy - m
        max_y = max(sy, ey) + hy + m
        min_z = min(sz, ez) - hz - m
        max_z = max(sz, ez) + hz + m
        for lo_x, lo_y, lo_z, hi_x, hi_y, hi_z, planes, brush in self.colliders:
            if (hi_x < min_x or lo_x > max_x or hi_y < min_y or lo_y > max_y
                    or hi_z < min_z or lo_z > max_z):
                continue
            _clip_to_solid(sx, sy, sz, ex, ey, ez, hx, hy, hz, planes, trace, brush)
            if trace.allsolid:
                break
        if not trace.allsolid and self.terrain is not None:
            _clip_to_terrain(sx, sy, sz, ex, ey, ez, hy, self.terrain, trace)

        if trace.allsolid:
            trace.fraction = 0.0
            trace.endpos = (sx, sy, sz)
        elif trace.fraction < 1.0:
            f = trace.fraction
            trace.endpos = (sx + (ex - sx) * f, sy + (ey - sy) * f, sz + (ez - sz) * f)
        else:
            trace.endpos = (ex, ey, ez)
        return trace

    def position_is_solid(self, pos, half):
        """True when a box of *half* centred on *pos* is inside a solid."""
        return self.trace(pos, pos, half).allsolid

    def point_is_solid(self, point):
        return self.trace(point, point, (0.0, 0.0, 0.0)).allsolid

    def nudge_out(self, pos, half, max_passes=4):
        """Push a box that is inside a solid back out; return the new centre.

        Quake 2 never has to do this: its movers push whatever they move into.
        Fio's movers and doors carry a rider but do not push, so one can end a
        tick overlapping the player. Each pass moves the box out through the
        nearest face of the solid it is deepest in (the face of greatest
        signed distance, i.e. the shortest way out), then terrain lifts it onto
        the surface. Returns *pos* unchanged when the box is already clear.
        """
        x, y, z = (float(v) for v in pos)
        hx, hy, hz = (float(v) for v in half)
        eps = PM_DIST_EPSILON * 2.0
        for _ in range(max_passes):
            best = None
            for lo_x, lo_y, lo_z, hi_x, hi_y, hi_z, planes, _brush in self.colliders:
                if (hi_x <= x - hx or lo_x >= x + hx or hi_y <= y - hy
                        or lo_y >= y + hy or hi_z <= z - hz or lo_z >= z + hz):
                    continue
                exit_sd = -math.inf
                exit_n = None
                for nx, ny, nz, d in planes:
                    d += abs(nx) * hx + abs(ny) * hy + abs(nz) * hz
                    sd = nx * x + ny * y + nz * z - d
                    if sd > 0.0:
                        exit_n = None
                        break           # outside this face: not inside the solid
                    if sd > exit_sd:
                        exit_sd = sd
                        exit_n = (nx, ny, nz)
                if exit_n is None:
                    continue
                if best is None or exit_sd < best[0]:
                    best = (exit_sd, exit_n)
            if best is None:
                break
            depth = -best[0] + eps
            x += best[1][0] * depth
            y += best[1][1] * depth
            z += best[1][2] * depth
        if self.terrain is not None:
            h = self.terrain.get_height_at_safe(x, z)
            if h is not None and y - hy < h - 0.01:
                y = h + hy
        return (x, y, z)
