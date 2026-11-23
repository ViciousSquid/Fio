"""A LightVolume's box and light source as a 2D view edits them.

Pure geometry, no Qt: a 2D view shows two world axes (*i1* across, *i2*
up its screen, as ``View2D._axis_indices`` gives them) and these functions
work in those world coordinates. The view turns them into pixels.

The box has eight handles in a view -- its four corners and the middle of
each edge -- keyed by which side of each axis they move: ``(-1, 0)`` is the
low edge on *i1*, ``(1, 1)`` the corner at the high end of both, ``0`` an
axis the handle leaves alone. Dragging one moves those sides to the cursor
and keeps the opposite ones, as a brush's handles do. The light source is
one more point, dragged freely in the view's plane.
"""

from __future__ import annotations

#: Every handle's key: four corners, then the four edge midpoints.
HANDLE_KEYS = (
    (-1, -1), (1, -1), (-1, 1), (1, 1),
    (0, -1), (0, 1), (-1, 0), (1, 0),
)


def outline(volume, i1, i2):
    """``(u0, v0, u1, v1)``: the box's extent on the view's two axes."""
    lo, hi = volume.get_bounds()
    return lo[i1], lo[i2], hi[i1], hi[i2]


def handles(volume, i1, i2):
    """``[(key, (u, v))]``: each handle of the box in this view."""
    u0, v0, u1, v1 = outline(volume, i1, i2)
    um, vm = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    pick = {-1: (u0, v0), 0: (um, vm), 1: (u1, v1)}
    return [(key, (pick[key[0]][0], pick[key[1]][1])) for key in HANDLE_KEYS]


def handle_at(volume, i1, i2, point, tolerance):
    """The key of the handle within *tolerance* (world units) of *point*,
    nearest first, or None."""
    best = None
    for key, (u, v) in handles(volume, i1, i2):
        d = max(abs(point[0] - u), abs(point[1] - v))
        if d <= tolerance and (best is None or d < best[0]):
            best = (d, key)
    return None if best is None else best[1]


def _snap(value, grid):
    if not grid:
        return float(value)
    return round(float(value) / grid) * grid


def resize(volume, key, point, i1, i2, grid=None):
    """``(pos, size)`` with the sides *key* names moved to *point* (snapped
    to *grid* when given). The opposite sides stay where they are; a side
    dragged past its opposite swaps with it; no extent falls below the
    volume's MIN_SIZE. The axis the view does not show is unchanged."""
    lo, hi = volume.get_bounds()
    lo, hi = list(lo), list(hi)
    minimum = float(getattr(volume, 'MIN_SIZE', 8.0))
    for axis, side, coord in ((i1, key[0], point[0]), (i2, key[1], point[1])):
        if side == 0:
            continue
        coord = _snap(coord, grid)
        a, b = (coord, hi[axis]) if side < 0 else (lo[axis], coord)
        if b < a:
            a, b = b, a
        if b - a < minimum:
            if side < 0:
                a = b - minimum
            else:
                b = a + minimum
        lo[axis], hi[axis] = a, b
    pos = [(a + b) / 2.0 for a, b in zip(lo, hi)]
    size = [b - a for a, b in zip(lo, hi)]
    return pos, size


def source_point(volume, i1, i2):
    """The light source's ``(u, v)`` in this view."""
    s = volume.get_source_position()
    return s[i1], s[i2]


def source_at(volume, i1, i2, point, tolerance):
    """Whether *point* is within *tolerance* of the light source marker."""
    u, v = source_point(volume, i1, i2)
    return max(abs(point[0] - u), abs(point[1] - v)) <= tolerance


def move_source(volume, point, i1, i2, grid=None):
    """The ``source_offset`` that puts the light source at *point* in this
    view (snapped to *grid* when given); its depth in the view is kept."""
    offset = list(volume.get_source_offset())
    centre = [float(c) for c in volume.pos]
    offset[i1] = _snap(point[0], grid) - centre[i1]
    offset[i2] = _snap(point[1], grid) - centre[i2]
    return offset
