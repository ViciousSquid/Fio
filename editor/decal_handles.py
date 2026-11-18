"""A selected decal's resize handles in the 2D views.

A decal is a box: *width* across its face, *height* up it, *depth* into the
surface (``engine.decal_table.placed_row``, the box the renderer paints).
In a 2D view its face is outlined and the edges of its width and height carry
a handle each -- those of the two axes the view does not see end-on (a decal
on a north wall, from above, shows only its width). Dragging a handle moves
that edge, the opposite one staying put:

* snapped to the grid: an edge along a world axis lands on a grid line, any
  other length is a whole number of grid steps;
* with "Keep aspect ratio" (the Decal tool's, on by default) the other side
  scales with it, about the decal's centre line.

Qt-free: the view hands in its two axes, the cursor and the grid.
"""

import math

import numpy as np

#: The box's two face axes, by the property that sizes them, and the column
#: of the box matrix that holds each.
FACE_AXES = (('width', 0), ('height', 1))
#: An axis seen more end-on than this (its length on screen, per unit) has
#: no handles in that view: dragging it would be guesswork.
MIN_ON_SCREEN = 0.2
#: Smallest side a drag leaves without the grid (world units).
MIN_SIDE = 1.0


def decal_frame(decal):
    """``(centre (3,), {'width': unit axis, 'height': unit axis},
    {'width': w, 'height': h})`` of *decal*'s face, or None when it has no
    size -- read from the box the renderer paints."""
    from engine.decal_table import placed_row
    row = placed_row(decal.pos, decal.properties, 0)
    if row is None:
        return None
    model = np.asarray(row[0], dtype=np.float64).reshape(4, 4).T
    centre = model[:3, 3].copy()
    axes, sizes = {}, {}
    for key, column in FACE_AXES:
        vector = model[:3, column]
        length = float(np.linalg.norm(vector))
        if length <= 0.0:
            return None
        axes[key] = vector / length
        sizes[key] = length
    return centre, axes, sizes


def _flat(vector, i1, i2):
    return np.array((vector[i1], vector[i2]), dtype=np.float64)


def outline(decal, i1, i2):
    """The face's four corners in the view's two axes (*i1*, *i2*: world
    axis indices), in order around it; None when it has no size."""
    frame = decal_frame(decal)
    if frame is None:
        return None
    centre, axes, sizes = frame
    w = axes['width'] * sizes['width'] * 0.5
    h = axes['height'] * sizes['height'] * 0.5
    return [tuple(_flat(centre + sw * w + sh * h, i1, i2))
            for sw, sh in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def handles(decal, i1, i2):
    """``[(key, sign, (u, v))]``: a handle on each edge the view can drag --
    the ends of the width (``key`` 'width') and of the height, *sign* +1 or
    -1 for which end -- at the edge's middle, in the view's axes."""
    frame = decal_frame(decal)
    if frame is None:
        return []
    centre, axes, sizes = frame
    found = []
    for key, _column in FACE_AXES:
        axis = axes[key]
        if float(np.linalg.norm(_flat(axis, i1, i2))) < MIN_ON_SCREEN:
            continue
        for sign in (1, -1):
            point = centre + sign * axis * sizes[key] * 0.5
            found.append((key, sign, tuple(_flat(point, i1, i2))))
    return found


def resize(decal, key, sign, cursor, i1, i2, grid=None, keep_aspect=True):
    """``(pos [x, y, z], width, height)``: *decal* with its *key* edge at
    *sign* end dragged to *cursor* (the view's ``(u, v)``), the other end
    fixed; None when it has no size. *grid* (or None: no snapping) snaps the
    moved edge; *keep_aspect* scales the other side with it."""
    frame = decal_frame(decal)
    if frame is None:
        return None
    centre, axes, sizes = frame
    axis = axes[key]
    flat = _flat(axis, i1, i2)
    flat_sq = float(flat @ flat)
    if flat_sq < MIN_ON_SCREEN * MIN_ON_SCREEN:
        return None
    fixed = centre - sign * axis * sizes[key] * 0.5
    # Along the axis from the fixed edge to the cursor, measured in the view.
    reach = float((np.asarray(cursor, dtype=np.float64) - _flat(fixed, i1, i2)) @ flat) / flat_sq
    reach *= sign
    minimum = MIN_SIDE
    if grid:
        grid = float(grid)
        minimum = grid
        world = _aligned_world_axis(axis)
        if world is not None:
            # The moved edge onto a grid line of that world axis.
            k, direction = world
            edge = fixed[k] + direction * sign * reach
            reach = (round(edge / grid) * grid - fixed[k]) * direction * sign
        else:
            reach = round(reach / grid) * grid
    side = max(reach, minimum)
    other = 'height' if key == 'width' else 'width'
    other_side = sizes[other] * side / sizes[key] if keep_aspect else sizes[other]
    new_centre = fixed + sign * axis * side * 0.5
    # The centre moves only along the dragged axis: the other side, scaled
    # or not, stays about the same centre line.
    width, height = (side, other_side) if key == 'width' else (other_side, side)
    offset = new_centre - centre
    pos = [float(p) + float(o) for p, o in zip(decal.pos, offset)]
    return pos, float(width), float(height)


def _aligned_world_axis(axis, tolerance=1e-4):
    """``(world axis index, +1 or -1)`` when *axis* runs along one, else
    None."""
    for k in range(3):
        if abs(abs(float(axis[k])) - 1.0) <= tolerance:
            return k, (1.0 if axis[k] > 0 else -1.0)
    return None


def handle_at(decal, i1, i2, point, radius):
    """The ``(key, sign)`` of the handle within *radius* of *point* (both in
    the view's axes, world units), nearest first; None."""
    best, best_d = None, math.inf
    for key, sign, at in handles(decal, i1, i2):
        d = math.hypot(at[0] - point[0], at[1] - point[1])
        if d <= radius and d < best_d:
            best, best_d = (key, sign), d
    return best
