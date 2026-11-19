"""The solid column of a triangle mesh over a point: how physics reads a
model's actual shape.

The physics engine (engine.physics.soma) meets most things as boxes. What is
not its box -- an angled brush -- it asks one question of: over this point,
between what heights is it solid (``SpatialGrid.columns``)? A body stands on
the top of that column, and is stopped by one that rises too steeply in front
of it. This module answers the same question for a model's collision mesh,
which is what "Collision Shape: Mesh" on a Prop means to physics.

Vertical lines through the mesh: the triangles a line passes through give the
heights it enters and leaves the model at, and the column runs from the lowest
to the highest of them. A model with a hollow under an overhang is solid
through the hollow -- a body rests on a table's top, it does not roll under
it -- which is the same simplification an angled brush's column makes.

Pure NumPy; the arrays are built once per mesh (:func:`prepare`).
"""

from __future__ import annotations

import numpy as np

#: Triangles whose projection onto the ground is thinner than this (world
#: units squared) are walls seen edge on: no vertical line passes through.
_FLAT = 1e-9
#: How far outside a triangle's edge a point still counts as on it: neighbours
#: must not leave a seam a support point can fall through.
_EDGE = 1e-6
#: Points x triangles tested at once; a larger mesh is walked in pieces.
_CHUNK = 1 << 20


def prepare(triangles):
    """*triangles* -- ``[((v0, v1, v2), normal), ...]`` as a collision proxy
    holds them, or an ``(n, 3, 3)`` array -- as the arrays :func:`column`
    reads, or None for no usable triangle."""
    if triangles is None:
        return None
    if isinstance(triangles, np.ndarray):
        points = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 3)
    else:
        if not len(triangles):
            return None
        points = np.asarray([tri[0] for tri in triangles],
                            dtype=np.float64).reshape(-1, 3, 3)
    if not len(points) or not np.isfinite(points).all():
        return None
    v0, v1, v2 = points[:, 0], points[:, 1], points[:, 2]
    e1x, e1z = v1[:, 0] - v0[:, 0], v1[:, 2] - v0[:, 2]
    e2x, e2z = v2[:, 0] - v0[:, 0], v2[:, 2] - v0[:, 2]
    det = e1x * e2z - e1z * e2x
    keep = np.abs(det) > _FLAT
    if not keep.any():
        return None
    inv = 1.0 / det[keep]
    return (v0[keep, 0], v0[keep, 2], v0[keep, 1],
            e1x[keep], e1z[keep], (v1[:, 1] - v0[:, 1])[keep],
            e2x[keep], e2z[keep], (v2[:, 1] - v0[:, 1])[keep], inv)


def column(prepared, x, z, shift=(0.0, 0.0, 0.0)):
    """``(bottom, top)`` of the mesh over each point ``(x, z)``: NaN where the
    vertical line at the point misses it. *shift* is how far the mesh has
    moved since it was prepared (a physics body's, from where it was made)."""
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    bottom = np.full(x.shape, np.nan)
    top = np.full(x.shape, np.nan)
    if prepared is None or not x.size:
        return bottom, top
    x0, z0, y0, e1x, e1z, dy1, e2x, e2z, dy2, inv = prepared
    px = x.reshape(-1) - shift[0]
    pz = z.reshape(-1) - shift[2]
    low = np.full(px.shape, np.inf)
    high = np.full(px.shape, -np.inf)
    step = max(1, _CHUNK // max(1, len(px)))
    for first in range(0, len(x0), step):
        part = slice(first, first + step)
        dx = px[None, :] - x0[part, None]
        dz = pz[None, :] - z0[part, None]
        u = (dx * e2z[part, None] - dz * e2x[part, None]) * inv[part, None]
        v = (e1x[part, None] * dz - e1z[part, None] * dx) * inv[part, None]
        inside = (u >= -_EDGE) & (v >= -_EDGE) & (u + v <= 1.0 + _EDGE)
        if not inside.any():
            continue
        height = y0[part, None] + u * dy1[part, None] + v * dy2[part, None]
        low = np.minimum(low, np.where(inside, height, np.inf).min(axis=0))
        high = np.maximum(high, np.where(inside, height, -np.inf).max(axis=0))
    hit = np.isfinite(high)
    flat_bottom = bottom.reshape(-1)
    flat_top = top.reshape(-1)
    flat_bottom[hit] = low[hit] + shift[1]
    flat_top[hit] = high[hit] + shift[1]
    return bottom, top
