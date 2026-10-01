"""
Which actor is under the cursor: a ray test against the published tables.

The play view's entity picker (``QtGameView.begin_actor_pick``) asks this
module for the actor a screen ray hits. It reads the same dense projection the
renderer drew the frame from -- :class:`~engine.entity_table.EntityTable` for
the actors and :class:`~engine.render_table.RenderTable` for the walls in front
of them -- so a pick is a handful of vectorised operations over numeric
columns, with no walk over the scene's objects.

* **Actors** are the table's monster rows that are not hidden (Big World
  parks through ``hidden``, so a parked actor cannot be picked). Each is a
  sphere centred on its row position -- where the sprite pass centres the
  billboard -- with half the billboard's larger side as radius, so aiming at a
  tall sprite's head hits it.
* **Occluders** are the visible solid brushes (``CLASS_SHADOW_CASTER``: not a
  trigger, fog, water, glass or glow volume, not a subtract brush), tested as
  the boxes the renderer draws -- ``centre +/- half``, turned by the row's
  ``rot`` about its centre, so a swung door or a spinning mover occludes where
  it is drawn. An actor behind a wall is not clickable; a trigger volume around
  it does not get in the way. Terrain does not occlude.

The nearest actor hit in front of the nearest wall wins.
"""

from __future__ import annotations

import numpy as np

#: Smallest pick-sphere radius, so a tiny sprite can still be clicked.
MIN_PICK_RADIUS = 24.0

#: No actor was hit.
NO_SLOT = -1


def _as_vec(value):
    return np.asarray([float(value[0]), float(value[1]), float(value[2])],
                      dtype=np.float64)


def nearest_wall(ray_o, ray_d, render_table, limit=np.inf) -> float:
    """Distance along the ray to the first visible solid brush, or *limit*."""
    if render_table is None or not getattr(render_table, 'count', 0):
        return float(limit)
    from engine.render_table import CLASS_SHADOW_CASTER, rotation_matrices
    n = render_table.count
    solid = ((render_table.class_bits[:n] & CLASS_SHADOW_CASTER) != 0) \
        & ~render_table.hidden[:n]
    rows = np.flatnonzero(solid)
    if not len(rows):
        return float(limit)
    bounds = render_table.bounds[rows]
    half = bounds[:, 3:6]
    # Every ray in the box's own frame, where the box is centred at the origin
    # and axis-aligned: o' = R^T (o - c), d' = R^T d. Unrotated rows (almost
    # all of them) need only the translation.
    o = np.broadcast_to(_as_vec(ray_o) - bounds[:, 0:3], half.shape).copy()
    d = np.broadcast_to(_as_vec(ray_d), half.shape).copy()
    turned, r = rotation_matrices(render_table.rot[rows])
    if len(turned):
        o[turned] = np.einsum('nji,nj->ni', r, o[turned])
        d[turned] = np.einsum('nji,nj->ni', r, d[turned])
    lo, hi = -half, half
    with np.errstate(divide='ignore', invalid='ignore'):
        inv = 1.0 / d
        t1 = (lo - o) * inv
        t2 = (hi - o) * inv
    t_near = np.fmin(t1, t2)
    t_far = np.fmax(t1, t2)
    # An axis the ray runs parallel to: inside the slab it constrains nothing,
    # outside it the box is missed.
    flat = d == 0.0
    if flat.any():
        inside = (o >= lo) & (o <= hi)
        t_near[flat] = np.where(inside[flat], -np.inf, np.inf)
        t_far[flat] = np.where(inside[flat], np.inf, -np.inf)
    enter = np.max(t_near, axis=1)
    leave = np.min(t_far, axis=1)
    hit = (enter <= leave) & (leave > 0.0)
    if not hit.any():
        return float(limit)
    # A box the eye is inside does not occlude what is in front of the eye.
    hit &= enter > 0.0
    if not hit.any():
        return float(limit)
    return float(min(limit, enter[hit].min()))


def pick_actor(ray_o, ray_d, entity_table, render_table=None,
               min_radius: float = MIN_PICK_RADIUS) -> int:
    """The entity-table slot of the actor the ray hits first, or ``NO_SLOT``.

    *ray_d* must be unit length. Brushes in *render_table* occlude; pass None
    to ignore walls.
    """
    if entity_table is None:
        return NO_SLOT
    slots = np.asarray(getattr(entity_table, 'monster_slots', ()), dtype=np.intp)
    if not len(slots):
        return NO_SLOT
    slots = slots[~entity_table.hidden[slots]]
    if not len(slots):
        return NO_SLOT
    o, d = _as_vec(ray_o), _as_vec(ray_d)
    centres = entity_table.pos[slots].astype(np.float64)
    radius = np.maximum(entity_table.sprite_size[slots].max(axis=1) * 0.5,
                        float(min_radius)).astype(np.float64)
    oc = o - centres
    b = oc @ d
    c = np.einsum('ij,ij->i', oc, oc) - radius * radius
    disc = b * b - c
    hit = disc >= 0.0
    if not hit.any():
        return NO_SLOT
    root = np.sqrt(np.where(hit, disc, 0.0))
    t = -b - root
    # The eye inside a sphere: the far crossing is the one in front of it.
    t = np.where(t > 0.0, t, -b + root)
    hit &= t > 0.0
    if not hit.any():
        return NO_SLOT
    wall = nearest_wall(o, d, render_table)
    hit &= t < wall
    if not hit.any():
        return NO_SLOT
    candidates = np.flatnonzero(hit)
    return int(slots[candidates[np.argmin(t[candidates])]])
