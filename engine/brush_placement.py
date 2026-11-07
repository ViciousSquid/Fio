"""Where a brush is and what it covers: one answer for every subsystem.

Stage 2 of the unified brush model (``docs/brush-model-stage2.md``). A brush
is four things, each with one owner:

* its **shape** -- a box's ``size``, or a convex brush's plane set;
* its **authored placement** -- where the mapper put it;
* its **runtime transform** -- a mover's displacement and spin;
* its **derived bounds** -- the shape, carried by the effective placement and
  turned by the spin.

This module is the one resolver every consumer reads the last three through:
rendering (the RenderTable's ``bounds``, the convex mesh anchor), main-view
culling, collision rows, the physics grid, Big World filing and picking. It
is GL-free (plain floats and NumPy) so the logic thread, the AI thread and
headless tests can all call it.

The rules (Stage 2a):

* **Box**: shape = ``size``; placement = ``pos``. Its box is ``pos +/- size/2``.
* **Mesh proxy** (an entity model's collision stand-in, built at play
  start: no planes, ``_collision_mode == 'mesh'``): its world-space
  triangle mesh is the shape, so its box is ``_mesh_bounds`` -- not the
  entity's ``pos`` and a placeholder ``size``.
* **Convex** (a valid plane set): the planes are world-space and are the
  authority for where the brush is at rest, so its box is the planes' own
  bounds -- never the ``pos``/``size`` copy the dict carries, which can drift
  (two shipped brushes by 128 units) or be written on its own (a ``size``-only
  write must not stretch what is drawn). A convex mover or door is displaced
  from that rest by its runtime transform. An invalid plane set has no shape
  of its own, so it falls back to ``pos``/``size``, exactly as it is drawn.
* **Spin**: a turned brush's derived bounds are the AABB of its turned box,
  ``|R| . half`` about its centre -- conservative, and the same rotation
  (axis-angle about the centre, Rodrigues) the renderer draws it with.
* **Authored bounds** carry no runtime transform and no spin: where a brush
  lives when nothing is moving it -- Big World's home cell.

Runtime transform (transitional, Stage 2a): movers still write their position
and spin into the brush dict (``pos``, ``_rot_angle``) as they always have,
so the runtime displacement of a convex mover is read as its ``pos`` against
its planes' rest centre (they are equal at rest: ``sync_brush_bounds`` keeps
them so, and a loaded map is normalised by :func:`normalise_loaded`), and a
spin from ``_rot_angle``/``rot_axis``. Stage 2b moves both into
:class:`~engine.mover_table.MoverTable`; the rules above do not change, only
where :func:`runtime_spin` and the displacement are read from.

Cost: a box costs two dict reads. A convex brush reads its cached
:class:`~engine.brush_geometry.ConvexGeometry` (``_geo_cache``, dropped by
every geometry invalidation) and only re-solves on a miss, through
:func:`engine.brush_geometry.get_convex`.
"""

from __future__ import annotations

import math

import numpy as np

from engine import brush_geometry

DEFAULT_POS = (0.0, 0.0, 0.0)
DEFAULT_SIZE = (64.0, 64.0, 64.0)
DEFAULT_AXIS = (0.0, 1.0, 0.0)

#: An axis shorter than this does not rotate (the renderer's own guard).
MIN_AXIS = 0.001


# ---------------------------------------------------------------------------
# The shape and its placement
# ---------------------------------------------------------------------------

def is_dynamic(brush):
    """A mover or a door: a brush with a runtime transform."""
    return bool(brush.get('is_mover') or brush.get('is_door'))


def shape_convex(brush):
    """The brush's valid :class:`ConvexGeometry`, or ``None`` for a box.

    The cached geometry when there is one (every geometry write drops it), so
    a per-tick reader does not re-hash the planes.
    """
    geo = brush.get('geometry')
    if not geo or not geo.get('planes'):
        return None
    convex = brush.get('_geo_cache')
    if convex is None:
        convex = brush_geometry.get_convex(brush)
    if convex is None or not convex.is_valid:
        return None
    return convex


def _mesh_box(brush):
    """A mesh proxy's box from its triangles' bounds, or ``None``."""
    if brush.get('_collision_mode') != 'mesh':
        return None
    bounds = brush.get('_mesh_bounds')
    if not bounds:
        return None
    lo, hi = bounds
    return ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, (lo[2] + hi[2]) * 0.5,
            (hi[0] - lo[0]) * 0.5, (hi[1] - lo[1]) * 0.5, (hi[2] - lo[2]) * 0.5)


def _dict_box(brush):
    pos = brush.get('pos') or DEFAULT_POS
    size = brush.get('size') or DEFAULT_SIZE
    return (float(pos[0]), float(pos[1]), float(pos[2]),
            size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)


def authored_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: the shape at its authored placement,
    unturned.

    A box: ``pos``, ``size / 2``. A convex brush: its planes' bounds. A
    mesh proxy: its triangles' bounds. (During
    Stage 2a a box mover's ``pos`` is still its runtime position; Stage 2b
    stops movers writing it.)
    """
    convex = shape_convex(brush)
    if convex is None:
        return _mesh_box(brush) or _dict_box(brush)
    return convex.shape_box


def placed_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: the shape where it is now, unturned.

    The authored box displaced by the runtime transform: a convex mover's
    centre is its live ``pos`` (see the module docstring), every other
    convex brush stays on its planes.
    """
    convex = shape_convex(brush)
    if convex is None:
        return _mesh_box(brush) or _dict_box(brush)
    box = convex.shape_box
    if is_dynamic(brush):
        pos = brush.get('pos')
        if pos is not None:
            return (float(pos[0]), float(pos[1]), float(pos[2]),
                    box[3], box[4], box[5])
    return box


def runtime_spin(brush):
    """``(ax, ay, az, angle_degrees)`` of a turned brush, or ``None``.

    The same values, and the same defaults, the RenderTable's ``rot`` column
    takes for the row.
    """
    angle = brush.get('_rot_angle') or 0.0
    if not angle:
        return None
    axis = brush.get('rot_axis') or DEFAULT_AXIS
    return (axis[0], axis[1], axis[2], angle)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def rotation_matrix(axis, angle_degrees):
    """The 3x3 rotation (rows) about *axis* by *angle_degrees*, or ``None``
    when it does not rotate (zero angle, degenerate axis)."""
    if not angle_degrees:
        return None
    ax, ay, az = float(axis[0]), float(axis[1]), float(axis[2])
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length <= MIN_AXIS:
        return None
    ax, ay, az = ax / length, ay / length, az / length
    theta = math.radians(float(angle_degrees))
    c = math.cos(theta)
    s = math.sin(theta)
    t = 1.0 - c
    return ((c + ax * ax * t, ax * ay * t - az * s, ax * az * t + ay * s),
            (ay * ax * t + az * s, c + ay * ay * t, ay * az * t - ax * s),
            (az * ax * t - ay * s, az * ay * t + ax * s, c + az * az * t))


def turned_half(hx, hy, hz, axis, angle_degrees):
    """Half-extents of the AABB of a ``(hx, hy, hz)`` box turned about its
    centre: ``|R| . half``."""
    r = rotation_matrix(axis, angle_degrees)
    if r is None:
        return hx, hy, hz
    (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = r
    return (abs(r00) * hx + abs(r01) * hy + abs(r02) * hz,
            abs(r10) * hx + abs(r11) * hy + abs(r12) * hz,
            abs(r20) * hx + abs(r21) * hy + abs(r22) * hz)


def rotation_matrices(rot):
    """The rows of a ``rot`` column block that rotate, and their 3x3 matrices.

    *rot* is ``(n, 4)``: axis xyz and an angle in degrees, about the row's
    centre. Returns ``(rows, R)`` with ``R`` shaped ``(len(rows), 3, 3)`` as
    (row, col). A zero angle or a degenerate axis means no rotation, exactly
    as the scalar path's ``if glm.length(axis) > 0.001`` guard decided, and
    such rows are left out. Shared by the draw's model matrices and anything
    else that must see a brush where the renderer drew it.
    """
    rot = np.asarray(rot)
    rotated = np.flatnonzero(rot[:, 3] != 0.0)
    if not len(rotated):
        return rotated, np.empty((0, 3, 3), dtype=np.float64)
    axis = rot[rotated, :3].astype(np.float64)
    length = np.linalg.norm(axis, axis=1)
    usable = length > MIN_AXIS
    rotated = rotated[usable]
    if not len(rotated):
        return rotated, np.empty((0, 3, 3), dtype=np.float64)
    axis = axis[usable] / length[usable, None]
    angle = np.radians(rot[rotated, 3].astype(np.float64))
    cos_a = np.cos(angle)[:, None]
    sin_a = np.sin(angle)[:, None]
    one_c = 1.0 - cos_a
    ux, uy, uz = axis[:, 0:1], axis[:, 1:2], axis[:, 2:3]
    # Rodrigues, as (rows, cols) of the 3x3 rotation.
    r = np.empty((len(rotated), 3, 3), dtype=np.float64)
    r[:, 0, 0] = (cos_a + ux * ux * one_c)[:, 0]
    r[:, 0, 1] = (ux * uy * one_c - uz * sin_a)[:, 0]
    r[:, 0, 2] = (ux * uz * one_c + uy * sin_a)[:, 0]
    r[:, 1, 0] = (uy * ux * one_c + uz * sin_a)[:, 0]
    r[:, 1, 1] = (cos_a + uy * uy * one_c)[:, 0]
    r[:, 1, 2] = (uy * uz * one_c - ux * sin_a)[:, 0]
    r[:, 2, 0] = (uz * ux * one_c - uy * sin_a)[:, 0]
    r[:, 2, 1] = (uz * uy * one_c + ux * sin_a)[:, 0]
    r[:, 2, 2] = (cos_a + uz * uz * one_c)[:, 0]
    return rotated, r


def turned_bounds(bounds, rot):
    """``(n, 6)`` centre|half rows with every turned row's half replaced by
    the half of its turned AABB (``|R| . half``).

    *bounds* is returned as it is when no row turns -- the common case, at
    the cost of one comparison over the ``rot`` angles -- and copied
    otherwise, so a table's own column is never written.
    """
    rows, r = rotation_matrices(rot)
    if not len(rows):
        return bounds
    out = np.array(bounds, dtype=np.float64)
    out[rows, 3:6] = np.einsum('nij,nj->ni', np.abs(r), out[rows, 3:6])
    return out


# ---------------------------------------------------------------------------
# Derived bounds
# ---------------------------------------------------------------------------

def is_plain_box(brush):
    """A box with nothing but ``pos``/``size`` to say where it is: no plane
    set, no mesh, no spin -- most of a level. Its derived bounds are
    ``pos +/- size/2``, which the hot paths below compute with no further
    lookups (and callers in tight loops may compute inline, as the box rule
    is exactly that)."""
    return ('geometry' not in brush and '_mesh_bounds' not in brush
            and not brush.get('_rot_angle'))


def world_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: the derived world AABB as centre and
    half -- where the brush is now, its spin included."""
    if is_plain_box(brush):
        pos = brush.get('pos') or DEFAULT_POS
        size = brush.get('size') or DEFAULT_SIZE
        return (float(pos[0]), float(pos[1]), float(pos[2]),
                size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
    cx, cy, cz, hx, hy, hz = placed_box(brush)
    spin = runtime_spin(brush)
    if spin is not None:
        hx, hy, hz = turned_half(hx, hy, hz, spin[:3], spin[3])
    return (cx, cy, cz, hx, hy, hz)


def world_bounds(brush):
    """``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``: the derived world AABB --
    where the brush is now, its spin included. The bound every broad phase,
    cull and index uses."""
    if not brush.get('_rot_angle'):
        if 'geometry' not in brush:
            if '_mesh_bounds' not in brush:             # is_plain_box, inline
                pos = brush.get('pos') or DEFAULT_POS
                size = brush.get('size') or DEFAULT_SIZE
                hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
                return (pos[0] - hx, pos[1] - hy, pos[2] - hz,
                        pos[0] + hx, pos[1] + hy, pos[2] + hz)
        elif not (brush.get('is_mover') or brush.get('is_door')):
            # A still convex brush: its shape's own bounds, cached on it.
            convex = brush.get('_geo_cache')
            if convex is not None and convex.is_valid:
                return convex.shape_bounds
    cx, cy, cz, hx, hy, hz = world_box(brush)
    return (cx - hx, cy - hy, cz - hz, cx + hx, cy + hy, cz + hz)


def authored_bounds(brush):
    """``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)`` with no runtime transform:
    where the brush lives when nothing moves it (Big World's home cell)."""
    if 'geometry' not in brush and '_mesh_bounds' not in brush:
        pos = brush.get('pos') or DEFAULT_POS
        size = brush.get('size') or DEFAULT_SIZE
        hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
        return (pos[0] - hx, pos[1] - hy, pos[2] - hz, pos[0] + hx, pos[1] + hy, pos[2] + hz)
    cx, cy, cz, hx, hy, hz = authored_box(brush)
    return (cx - hx, cy - hy, cz - hz, cx + hx, cy + hy, cz + hz)


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def ray_to_shape(brush, ray_o, ray_d):
    """``(origin, direction)`` of a world ray in the frame the brush's shape
    is authored in -- its runtime transform undone.

    A point ``s`` of the authored shape is drawn at ``P + R (s - A)``, with
    ``A`` the authored centre, ``P`` the placed centre and ``R`` the spin; so
    the ray maps by ``s = A + R^T (w - P)``, ``d' = R^T d``. Rigid, so a
    distance along the ray is the same in both frames. A box tested against
    :func:`authored_box`, or a convex brush against its planes, in this frame
    is hit exactly where it is drawn.
    """
    ax, ay, az = authored_box(brush)[:3]
    px, py, pz = placed_box(brush)[:3]
    ox = float(ray_o[0]) - px
    oy = float(ray_o[1]) - py
    oz = float(ray_o[2]) - pz
    dx, dy, dz = float(ray_d[0]), float(ray_d[1]), float(ray_d[2])
    spin = runtime_spin(brush)
    r = rotation_matrix(spin[:3], spin[3]) if spin is not None else None
    if r is not None:
        (r00, r01, r02), (r10, r11, r12), (r20, r21, r22) = r
        ox, oy, oz = (r00 * ox + r10 * oy + r20 * oz,
                      r01 * ox + r11 * oy + r21 * oz,
                      r02 * ox + r12 * oy + r22 * oz)
        dx, dy, dz = (r00 * dx + r10 * dy + r20 * dz,
                      r01 * dx + r11 * dy + r21 * dz,
                      r02 * dx + r12 * dy + r22 * dz)
    return (ax + ox, ay + oy, az + oz), (dx, dy, dz)


def ray_box(box, ray_o, ray_d, eps=1e-6):
    """Entry distance (``>= 0``) of a ray into a ``(cx, cy, cz, hx, hy, hz)``
    box, or ``None`` when it misses -- the slab test the editor's pickers
    use (a ray starting inside enters at 0)."""
    t_min, t_max = 0.0, float('inf')
    for axis in range(3):
        o = ray_o[axis]
        d = ray_d[axis]
        lo = box[axis] - box[axis + 3]
        hi = box[axis] + box[axis + 3]
        if abs(d) < eps:
            if o < lo or o > hi:
                return None
            continue
        t1 = (lo - o) / d
        t2 = (hi - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        if t1 > t_min:
            t_min = t1
        if t2 < t_max:
            t_max = t2
        if t_min > t_max:
            return None
    return t_min


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def normalise_loaded(brush):
    """Make a loaded convex brush's ``pos``/``size`` copy equal its planes'
    bounds -- the planes are where it is.

    Returns the distance the stored centre moved (0.0 for a box, an invalid
    plane set, or a copy that was already right). The planes are not
    touched, so what is drawn does not change. A play-time rest
    (``original_pos``) a map kept is left alone: no shipped convex brush
    both carries one and has drifted, and Stage 2b stops play state being
    kept in the dict at all.
    """
    convex = shape_convex(brush)
    if convex is None:
        return 0.0
    cx, cy, cz, hx, hy, hz = convex.shape_box
    pos = brush.get('pos') or DEFAULT_POS
    moved = math.sqrt((float(pos[0]) - cx) ** 2 + (float(pos[1]) - cy) ** 2
                      + (float(pos[2]) - cz) ** 2)
    brush_geometry.sync_brush_bounds(brush)
    return moved
