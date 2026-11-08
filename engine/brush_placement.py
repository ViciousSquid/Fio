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

The rules:

* **Box**: shape = ``size``; placement = ``pos``. Its box is ``pos +/- size/2``.
* **Mesh proxy** (an entity model's collision stand-in, built at play
  start: no planes, ``_collision_mode == 'mesh'``): its world-space
  triangle mesh is the shape, so its box is ``_mesh_bounds`` -- not the
  entity's ``pos`` and a placeholder ``size``.
* **Convex** (a valid plane set): the planes are in the brush's **local
  frame**, centred on their own bounds, and ``pos`` places them (Stage 2d,
  map version 5): its box is ``pos`` plus the planes' own bounds -- never the
  ``size`` copy the dict carries (it is not saved, and a ``size``-only write
  must not stretch what is drawn). An invalid plane set has no shape of its
  own, so it falls back to ``pos``/``size``, exactly as it is drawn.
* **Spin**: a turned brush's derived bounds are the AABB of its turned box,
  ``|R| . half`` about its centre -- conservative, and the same rotation
  (axis-angle about the centre, Rodrigues) the renderer draws it with.
* **Authored bounds** carry no runtime transform and no spin: where a brush
  lives when nothing is moving it -- Big World's home cell.

**Runtime transform** (Stage 2b). Where a mover's or door's centre is now,
and its spin, are owned by a *runtime source* and never written into the
brush dict: ``pos`` is the authored placement, always. The
sources are registered here (:func:`register_runtime`): the logic thread's
:class:`~engine.mover_table.MoverTable` during play, and the editor's mover
preview (:data:`PREVIEW`, a :class:`RuntimeOverrides`). Only a brush flagged
``is_mover`` or ``is_door`` is looked up, so every other brush pays two dict
reads for the question. The play-time keys movers used to write into the
dict (:data:`PLAY_STATE_KEYS`) are no longer read; :func:`drop_play_state`
removes them from a loaded record and the serialiser never writes them.

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
    """The brush's valid shape -- a :class:`ConvexGeometry` in its **local
    frame** -- or ``None`` for a box.

    The cached geometry when there is one (every geometry write drops it,
    and a move never does), so a per-tick reader does not re-hash the
    planes.
    """
    geo = brush.get('geometry')
    if not geo or not geo.get('planes'):
        return None
    convex = brush.get('_geo_cache')
    if convex is None:
        convex = brush_geometry.local_convex(brush)
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

    A box: ``pos``, ``size / 2``. A convex brush: ``pos`` plus its local
    planes' bounds. A mesh proxy: its triangles' bounds.
    """
    convex = shape_convex(brush)
    if convex is None:
        return _mesh_box(brush) or _dict_box(brush)
    cx, cy, cz, hx, hy, hz = convex.shape_box
    pos = brush.get('pos') or DEFAULT_POS
    return (float(pos[0]) + cx, float(pos[1]) + cy, float(pos[2]) + cz, hx, hy, hz)


def shape_origin(brush):
    """Where the shape's centre is in the coordinates its planes are given
    in: a convex brush's local centre (``~0``), or -- for a box, whose planes
    are implied at its authored placement -- its authored centre. What
    :class:`Frame` and :func:`ray_to_shape` carry a world query to."""
    convex = shape_convex(brush)
    if convex is not None:
        return convex.shape_box[:3]
    return authored_box(brush)[:3]


def placed_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: the shape where it is now, unturned --
    the authored box carried to the runtime transform's centre."""
    box = authored_box(brush)
    runtime = runtime_of(brush)
    if runtime is None:
        return box
    return (runtime[0], runtime[1], runtime[2], box[3], box[4], box[5])


def runtime_spin(brush):
    """``(ax, ay, az, angle_degrees)`` of a turned brush, or ``None``.

    The values the RenderTable's ``rot`` column takes for the row.
    """
    runtime = runtime_of(brush)
    if runtime is None or not runtime[6]:
        return None
    return runtime[3:7]


# ---------------------------------------------------------------------------
# Runtime sources
# ---------------------------------------------------------------------------

#: Weak references to the registered runtime sources. Replaced, never
#: mutated, so a reader on another thread (line of sight runs on the AI
#: thread) always iterates a whole tuple.
_sources = ()


def register_runtime(source):
    """Add *source* to the sources the resolver consults. Held weakly: a
    source that is garbage collected is dropped.

    A source keeps ``runtime_entries``, a dict ``id(brush) -> (brush,
    (cx, cy, cz, ax, ay, az, angle))`` -- the brush itself, so a reused id
    never answers for another, and its runtime transform. The resolver reads
    it directly (the question is asked in every broad phase), so a source
    changes it only by single dict operations or by replacing the whole
    dict, either of which a reader on another thread sees whole.

    ``(cx, cy, cz)`` is where the shape's centre is *now*, absolute rather
    than a displacement: the mover table hands over its position column as
    it is, so every reader sees the exact value the renderer is published
    (``authored + (now - authored)`` is not always ``now`` in floating
    point)."""
    import weakref
    global _sources
    live = tuple(ref for ref in _sources if ref() is not None and ref() is not source)
    _sources = live + (weakref.ref(source),)


def unregister_runtime(source):
    global _sources
    _sources = tuple(ref for ref in _sources
                     if ref() is not None and ref() is not source)


def runtime_of(brush):
    """``(cx, cy, cz, ax, ay, az, angle_degrees)``: a mover's or door's
    runtime transform -- its centre now and its spin -- or ``None`` when
    nothing moves it. The first registered source that knows the brush
    answers."""
    if not (brush.get('is_mover') or brush.get('is_door')):
        return None
    key = id(brush)
    for ref in _sources:
        source = ref()
        if source is None:
            continue
        entry = source.runtime_entries.get(key)
        if entry is not None and entry[0] is brush:
            return entry[1]
    return None


class RuntimeOverrides:
    """A runtime source holding explicit transforms by brush.

    The editor's mover preview drives one (:data:`PREVIEW`) instead of
    writing the brush's ``pos`` or spin; tests drive their own. Entries keep
    their brush alive, so an id is never reused while it is answered for.
    """

    def __init__(self):
        self.runtime_entries = {}

    def set(self, brush, offset=(0.0, 0.0, 0.0), spin=None, centre=None):
        """Carry *brush* to *centre* (or its authored centre plus *offset*)
        and turn it by *spin* (``(axis, angle_degrees)``) about it."""
        if centre is None:
            home = authored_box(brush)
            centre = (home[0] + float(offset[0]), home[1] + float(offset[1]),
                      home[2] + float(offset[2]))
        axis, angle = spin if spin is not None else ((0.0, 1.0, 0.0), 0.0)
        self.runtime_entries[id(brush)] = (brush, (
            float(centre[0]), float(centre[1]), float(centre[2]),
            float(axis[0]), float(axis[1]), float(axis[2]), float(angle)))

    def clear(self, brush=None):
        if brush is None:
            self.runtime_entries.clear()
        else:
            self.runtime_entries.pop(id(brush), None)

    def runtime_of(self, brush):
        entry = self.runtime_entries.get(id(brush))
        if entry is None or entry[0] is not brush:
            return None
        return entry[1]

    def __len__(self):
        return len(self.runtime_entries)


#: The editor's mover preview: registered for the life of the process.
PREVIEW = RuntimeOverrides()
register_runtime(PREVIEW)


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


#: ``(ax, ay, az, angle) -> |R|`` (nine floats, or ``()`` when it does not
#: turn), so a spinner's trig runs once per angle however many broad phases
#: ask for its box. Bounded; cleared whole when full. Values only, so a
#: reader on another thread can never see a wrong one.
_TURNS = {}
_TURNS_LIMIT = 4096


def _abs_rotation(axis, angle_degrees):
    key = (axis[0], axis[1], axis[2], angle_degrees)
    turn = _TURNS.get(key)
    if turn is None:
        r = rotation_matrix(axis, angle_degrees)
        turn = () if r is None else tuple(abs(v) for row in r for v in row)
        if len(_TURNS) >= _TURNS_LIMIT:
            _TURNS.clear()
        _TURNS[key] = turn
    return turn


def turned_half(hx, hy, hz, axis, angle_degrees):
    """Half-extents of the AABB of a ``(hx, hy, hz)`` box turned about its
    centre: ``|R| . half``."""
    if not angle_degrees:
        return hx, hy, hz
    turn = _abs_rotation(axis, angle_degrees)
    if not turn:
        return hx, hy, hz
    r00, r01, r02, r10, r11, r12, r20, r21, r22 = turn
    return (r00 * hx + r01 * hy + r02 * hz,
            r10 * hx + r11 * hy + r12 * hz,
            r20 * hx + r21 * hy + r22 * hz)


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
    set, no mesh, and not a mover or a door (only those have a runtime
    transform) -- most of a level. Its derived bounds are
    ``pos +/- size/2``, which the hot paths below compute with no further
    lookups (and callers in tight loops may compute inline, as the box rule
    is exactly that)."""
    return ('geometry' not in brush and '_mesh_bounds' not in brush
            and not (brush.get('is_mover') or brush.get('is_door')))


def world_box(brush):
    """``(cx, cy, cz, hx, hy, hz)``: the derived world AABB as centre and
    half -- where the brush is now, its spin included."""
    if ('geometry' not in brush and '_mesh_bounds' not in brush
            and not (brush.get('is_mover') or brush.get('is_door'))):
        # is_plain_box: no runtime source answers for it.
        pos = brush.get('pos') or DEFAULT_POS
        size = brush.get('size') or DEFAULT_SIZE
        return (float(pos[0]), float(pos[1]), float(pos[2]),
                size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
    return world_box_at(brush, runtime_of(brush))


def world_box_at(brush, runtime):
    """:func:`world_box` given the brush's :func:`runtime_of`, for a caller
    that has just asked for it."""
    if 'geometry' not in brush and '_mesh_bounds' not in brush:
        # A box (a mover or door, or no runtime): authored_box, inline.
        pos = brush.get('pos') or DEFAULT_POS
        size = brush.get('size') or DEFAULT_SIZE
        if runtime is None:
            return (float(pos[0]), float(pos[1]), float(pos[2]),
                    size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
        hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
    else:
        box = authored_box(brush)
        if runtime is None:
            return box
        hx, hy, hz = box[3], box[4], box[5]
    if runtime[6]:
        hx, hy, hz = turned_half(hx, hy, hz, runtime[3:6], runtime[6])
    return (runtime[0], runtime[1], runtime[2], hx, hy, hz)


def carried_box(box, runtime):
    """An :func:`authored_box` *box* where *runtime* puts it: what
    :func:`world_box_at` returns, for a caller holding the authored box."""
    if runtime is None:
        return box
    hx, hy, hz = box[3], box[4], box[5]
    if runtime[6]:
        hx, hy, hz = turned_half(hx, hy, hz, runtime[3:6], runtime[6])
    return (runtime[0], runtime[1], runtime[2], hx, hy, hz)


def world_bounds(brush):
    """``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``: the derived world AABB --
    where the brush is now, its spin included. The bound every broad phase,
    cull and index uses."""
    if not (brush.get('is_mover') or brush.get('is_door')):
        # No runtime transform: where it was authored.
        if 'geometry' not in brush:
            if '_mesh_bounds' not in brush:             # is_plain_box, inline
                pos = brush.get('pos') or DEFAULT_POS
                size = brush.get('size') or DEFAULT_SIZE
                hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
                return (pos[0] - hx, pos[1] - hy, pos[2] - hz,
                        pos[0] + hx, pos[1] + hy, pos[2] + hz)
        else:
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

    A point ``s`` of the shape is drawn at ``P + R (s - A)``, with ``A`` the
    shape's centre in its own coordinates (:func:`shape_origin`), ``P`` the
    placed centre and ``R`` the spin; so the ray maps by
    ``s = A + R^T (w - P)``, ``d' = R^T d``. Rigid, so a distance along the
    ray is the same in both frames. A box tested against :func:`authored_box`,
    or a convex brush against its local shape (:func:`shape_convex`), in this
    frame is hit exactly where it is drawn.
    """
    ax, ay, az = shape_origin(brush)
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
# Collision in the shape's frame (Stage 2c)
# ---------------------------------------------------------------------------

class Frame:
    """The map from world space into the frame a brush's shape is authored
    in, for one runtime transform: ``s = A + R^T (w - P)``, and back for a
    direction, ``n_w = R n``.

    Built once per runtime transform and cached on the brush (the mover
    table hands over a new transform tuple only when the row moves), so a
    collision query costs a few float operations and allocates no matrix.
    """

    __slots__ = ('ax', 'ay', 'az', 'px', 'py', 'pz', 'r')

    def __init__(self, authored, centre, rotation):
        self.ax, self.ay, self.az = authored
        self.px, self.py, self.pz = centre
        #: The rotation's nine entries row by row, or None for a translation.
        self.r = (None if rotation is None
                  else tuple(v for row in rotation for v in row))

    def to_shape(self, x, y, z):
        """A world point in the shape's frame."""
        dx, dy, dz = x - self.px, y - self.py, z - self.pz
        r = self.r
        if r is not None:
            dx, dy, dz = (r[0] * dx + r[3] * dy + r[6] * dz,
                          r[1] * dx + r[4] * dy + r[7] * dz,
                          r[2] * dx + r[5] * dy + r[8] * dz)
        return self.ax + dx, self.ay + dy, self.az + dz

    def dir_to_shape(self, x, y, z):
        """A world direction in the shape's frame (``R^T d``)."""
        r = self.r
        if r is None:
            return x, y, z
        return (r[0] * x + r[3] * y + r[6] * z,
                r[1] * x + r[4] * y + r[7] * z,
                r[2] * x + r[5] * y + r[8] * z)

    def to_world_dir(self, x, y, z):
        """A direction in the shape's frame, in world space (``R n``)."""
        r = self.r
        if r is None:
            return x, y, z
        return (r[0] * x + r[1] * y + r[2] * z,
                r[3] * x + r[4] * y + r[5] * z,
                r[6] * x + r[7] * y + r[8] * z)


#: ``(ax, ay, az, angle) -> rotation rows`` (or ``None``), memoised as
#: :data:`_TURNS` is: a spinner's frame is rebuilt once per angle, however
#: many queries ask, and its trigonometry runs once.
_ROTATIONS = {}
_MISSING = object()


def _rotation(axis, angle_degrees):
    key = (axis[0], axis[1], axis[2], angle_degrees)
    rotation = _ROTATIONS.get(key, _MISSING)
    if rotation is _MISSING:
        rotation = rotation_matrix(axis, angle_degrees)
        if len(_ROTATIONS) >= _TURNS_LIMIT:
            _ROTATIONS.clear()
        _ROTATIONS[key] = rotation
    return rotation


#: Runtime-only key this module caches on a brush dict (stripped on save,
#: like the geometry and AABB caches): its collision shape and frame, with
#: what they were built from.
RUNTIME_CACHE_KEYS = ('_collision_entry',)


def collision_shape(brush):
    """What the player collides with when *brush* is a solid shape:
    ``(planes, triangles, bounds, frame)``, or ``None`` for the AABB fast
    path.

    * a convex brush, static or moving: its shape's collision data
      (:meth:`ConvexGeometry.collision_shape`), cached on the shape;
    * a spinning box: its six planes;
    * an entity model's collision mesh proxy: its world-space mesh;
    * any other box -- static, or carried by a mover without turning --
      ``None``: its derived AABB is its exact shape.

    *planes*, *triangles* and *bounds* are in the coordinates the shape is
    given in (a convex brush's local frame); *frame* (a :class:`Frame`)
    carries a world query into them, ``None`` when they are world space.

    The answer is cached on the brush against what it is built from -- the
    runtime transform (a tuple its source replaces whenever the brush
    moves), the cached shape (dropped by every geometry edit) and the
    placement -- so the player's several questions per tick build it once,
    and a brush that does not move never builds it again.
    """
    convex = shape_convex(brush)
    runtime = runtime_of(brush)
    entry = brush.get('_collision_entry')
    if convex is not None:
        if runtime is None:
            pos = brush.get('pos') or DEFAULT_POS
            key = (pos[0], pos[1], pos[2])
        else:
            key = runtime
        if entry is not None and entry[1] is convex and (entry[0] is key or entry[0] == key):
            return entry[2]
        planes, triangles, bounds = convex.collision_shape()
        origin = convex.shape_box[:3]
        if runtime is None:
            frame = Frame(origin, authored_box(brush)[:3], None)
        else:
            frame = Frame(origin, runtime[:3], _rotation(runtime[3:6], runtime[6]))
        result = (planes, triangles, bounds, frame)
        brush['_collision_entry'] = (key, convex, result)
        return result
    if runtime is not None and runtime[6]:
        rotation = _rotation(runtime[3:6], runtime[6])
        if rotation is not None:
            box = authored_box(brush)
            if entry is not None and entry[0] is runtime and entry[1] == box:
                return entry[2]
            cx, cy, cz, hx, hy, hz = box
            planes = [(1.0, 0.0, 0.0, cx + hx), (-1.0, 0.0, 0.0, -(cx - hx)),
                      (0.0, 1.0, 0.0, cy + hy), (0.0, -1.0, 0.0, -(cy - hy)),
                      (0.0, 0.0, 1.0, cz + hz), (0.0, 0.0, -1.0, -(cz - hz))]
            bounds = ([cx - hx, cy - hy, cz - hz], [cx + hx, cy + hy, cz + hz])
            result = (planes, None, bounds, Frame(box[:3], runtime[:3], rotation))
            brush['_collision_entry'] = (runtime, box, result)
            return result
    if brush.get('_collision_mode') == 'mesh' and 'geometry' not in brush:
        return (brush.get('_mesh_planes') or [], brush.get('_mesh_triangles'),
                brush.get('_mesh_bounds'), None)
    return None


def collision_frame(brush):
    """The :class:`Frame` of the brush's :func:`collision_shape`, or
    ``None`` (no shape, or one given in world space)."""
    shape = collision_shape(brush)
    return None if shape is None else shape[3]


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

class Placement(tuple):
    """What :func:`placement` answers, read by name.

    ``authored`` -- the authored centre ``(x, y, z)`` (a box's ``pos``, a
    convex brush's planes' centre); ``centre`` -- where it is now, displaced
    by its runtime transform; ``spin`` -- ``(ax, ay, az, angle_degrees)`` or
    ``None``; ``half`` -- the shape's unturned half-extents; ``bounds`` --
    the derived world AABB ``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``.
    """
    __slots__ = ()
    _fields = ('authored', 'centre', 'spin', 'half', 'bounds')

    def __new__(cls, authored, centre, spin, half, bounds):
        return tuple.__new__(cls, (authored, centre, spin, half, bounds))

    authored = property(lambda self: self[0])
    centre = property(lambda self: self[1])
    spin = property(lambda self: self[2])
    half = property(lambda self: self[3])
    bounds = property(lambda self: self[4])

    def __repr__(self):
        return 'Placement(%s)' % ', '.join(
            '%s=%r' % (name, value) for name, value in zip(self._fields, self))


def placement(brush):
    """Where *brush* is: authored and live, its spin and its bounds -- the
    accessor plugins read a moving brush through (Plugin API 1.8.0), since
    ``brush['pos']`` is the authored placement and never moves in play."""
    authored = authored_box(brush)
    placed = placed_box(brush)
    return Placement(tuple(authored[:3]), tuple(placed[:3]), runtime_spin(brush),
                     tuple(placed[3:]), world_bounds(brush))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

#: Keys play used to write into the authored dict, which the serialiser kept
#: (seven shipped maps carry ``original_pos``, one ``rotation_yaw``). No
#: longer written or read.
PLAY_STATE_KEYS = ('original_pos', '_rot_angle', 'rotation_yaw')


def drop_play_state(brush):
    """Remove the play-time keys from a loaded record (in place).

    A box saved mid-motion (``pos`` differs from ``original_pos``) is put
    back where it was authored, at ``original_pos``; a convex brush's planes
    are its authored placement, so its ``pos`` is re-derived from them by
    :func:`normalise_loaded`. Returns the keys that were present.
    """
    found = [key for key in PLAY_STATE_KEYS if key in brush]
    if not found:
        return found
    rest = brush.get('original_pos')
    if rest is not None and 'geometry' not in brush:
        try:
            brush['pos'] = [float(rest[0]), float(rest[1]), float(rest[2])]
        except (TypeError, ValueError, IndexError):
            pass
    for key in found:
        brush.pop(key, None)
    return found


#: The map version whose angled brushes store their planes in their local
#: frame (Stage 2d). Version 4 (Stage 1) and earlier store world-space planes
#: and a ``pos``/``size`` copy of their bounds; loading localises them
#: (:func:`localise_legacy`), and ``engine.brush_faces``'s legacy export
#: writes them back.
MAP_VERSION = 5


def normalise_loaded(brush):
    """A loaded version-5 convex brush: re-derive the ``size`` copy (not
    saved) from its local shape. ``pos`` is its authored placement and is
    left alone. Returns 0.0."""
    if shape_convex(brush) is not None:
        brush_geometry.sync_brush_bounds(brush)
    return 0.0


def localise_legacy(brush):
    """A convex brush from a map before version 5: its world-space planes are
    re-expressed about their own bounds centre ``c`` (``d' = d - n . c``) and
    ``pos`` becomes ``c``, so it draws exactly where it did -- a stale
    ``pos``/``size`` copy (two shipped brushes, 128 units) cannot misplace it.
    An invalid plane set has no centre: it keeps its ``pos`` and its planes
    are taken relative to it. Returns the distance the stored ``pos`` moved
    (0.0 for a box). Replaces the plane list; never mutates the input's.
    """
    geo = brush.get('geometry')
    if not geo or not geo.get('planes'):
        return 0.0
    planes = [dict(p) for p in geo['planes']]
    world = brush_geometry.ConvexGeometry(planes)
    old = brush.get('pos') or DEFAULT_POS
    if world.is_valid:
        centre = world.center()
        origin = [float(centre[0]), float(centre[1]), float(centre[2])]
    else:
        origin = [float(old[0]), float(old[1]), float(old[2])]
    local = brush_geometry.translate_planes(planes, [-v for v in origin])
    geometry = dict(geo)
    geometry['planes'] = [brush_geometry._plane_to_json(p) for p in local]
    brush['geometry'] = geometry
    moved = math.sqrt(sum((float(old[i]) - origin[i]) ** 2 for i in range(3)))
    brush['pos'] = origin
    brush_geometry.invalidate_geometry_cache(brush)
    if world.is_valid:
        brush_geometry.sync_brush_bounds(brush)
    return moved

def world_layout(record):
    """A copy of a brush record laid out as map version 4 stores it: an
    angled brush's planes in world space (its local planes carried by its
    ``pos``) and its ``size`` -- what a build before Stage 2d (and the
    Android player) reads. Runtime and cache keys are left out; a box is
    copied as it is. For :func:`export_world_level` and the frozen reference
    renderer's test bridge -- never for a normal save."""
    import copy
    out = copy.deepcopy({k: v for k, v in record.items() if not str(k).startswith('_')
                         or k == '_io_connections'})
    geo = out.get('geometry')
    if not geo or not geo.get('planes'):
        return out
    pos = out.get('pos') or DEFAULT_POS
    world = brush_geometry.translate_planes(
        [dict(p) for p in geo['planes']], [float(pos[0]), float(pos[1]), float(pos[2])])
    geo['planes'] = [brush_geometry._plane_to_json(p) for p in world]
    convex = brush_geometry.ConvexGeometry(world)
    if convex.is_valid:
        cx, cy, cz, hx, hy, hz = convex.shape_box
        out['pos'] = [cx, cy, cz]
        out['size'] = [hx * 2.0, hy * 2.0, hz * 2.0]
    return out


def export_world_level(level):
    """A copy of a version-5 level document for builds that read version 4
    (and the Android player): every angled brush through
    :func:`world_layout`, ``version`` set to 4. Lossless: version 4 holds
    every face record and every placement."""
    import copy
    out = copy.deepcopy({k: v for k, v in level.items() if k != 'brushes'})
    out['version'] = MAP_VERSION - 1
    out['brushes'] = [world_layout(b) for b in level.get('brushes') or []]
    return out
