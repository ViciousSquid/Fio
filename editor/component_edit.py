"""Unified component selection and dragging — Radiant-style direct editing.

This module is the single place Fio answers four questions that used to have
(or would otherwise grow) four unrelated answers:

* what is under the cursor — an object, a face, an edge or a vertex?
* which side of a brush is the cursor asking to stretch?
* how does dragging that component change the brush?
* which brushes does a Radiant-style selection operation catch?

It reproduces the *workflow* a GtkRadiant mapper expects — point at geometry,
click it, drag it, use a modifier to change what the drag means — implemented
natively on Fio's own convex plane-set geometry.  There is no second geometry
representation: every query reads the ``ConvexGeometry`` that
:mod:`engine.brush_geometry` already caches per brush, and every edit goes back
through that module's plane helpers, so only the edited brush's caches are
touched.

Performance contract
--------------------
Everything here is **event driven**.  Nothing in this module is called from a
render loop:

* picking is invoked from mouse press / hover handlers only, and only ever
  tests the brushes it is given — the caller passes the current selection, so a
  component pick is O(selected brushes), never O(scene);
* a drag captures the affected brushes' state **once** at mouse-down and
  recomputes from that snapshot, so a mouse move does no scene-wide work and
  cannot accumulate drift;
* overlay geometry for highlighting is built on demand and cached behind a
  version counter (:meth:`ComponentController.version`), so a renderer can skip
  the rebuild entirely while nothing has changed.

The module is deliberately Qt-free (NumPy only) so it can be unit-tested
head-less and reused by both the 2D views and the 3D viewport.
"""

import math

import numpy as np

from engine import brush_geometry as bg


# --------------------------------------------------------------------------
# Component modes
# --------------------------------------------------------------------------

MODE_OBJECT = 'object'
MODE_FACE = 'face'
MODE_EDGE = 'edge'
MODE_VERTEX = 'vertex'

COMPONENT_MODES = (MODE_OBJECT, MODE_FACE, MODE_EDGE, MODE_VERTEX)

MODE_LABELS = {
    MODE_OBJECT: 'Object',
    MODE_FACE: 'Face',
    MODE_EDGE: 'Edge',
    MODE_VERTEX: 'Vertex',
}

# How close (in world units, converted from pixels by the caller) the cursor
# must be to a brush side before a press grabs it for a side stretch.  Radiant
# grabs a side from any distance, which in Fio would swallow the marquee drag
# and empty-space deselect, so the grab is banded instead — see
# :func:`side_select`.
DEFAULT_GRAB_TOLERANCE = 6.0


def _v3(seq):
    return np.asarray(seq, dtype=np.float64).reshape(3)


# --------------------------------------------------------------------------
# Component references
# --------------------------------------------------------------------------

class ComponentRef:
    """One addressable piece of one brush: a face, an edge or a vertex.

    ``indices`` are positions into the brush's corner array
    (:func:`brush_points`); ``plane`` is the index into the brush's plane set
    for a face component and ``None`` otherwise.  ``key`` gives the component a
    stable identity that survives a geometry rebuild: faces key off their plane
    index, vertices and edges off their quantised world position, so a
    selection can be re-resolved after an edit instead of silently pointing at
    whatever corner inherited the old index.
    """

    __slots__ = ('brush', 'kind', 'indices', 'plane', 'key', 'position')

    def __init__(self, brush, kind, indices, plane=None, position=None):
        self.brush = brush
        self.kind = kind
        self.indices = tuple(int(i) for i in indices)
        self.plane = plane
        self.position = (np.zeros(3) if position is None
                         else _v3(position))
        if kind == MODE_FACE:
            self.key = ('face', int(plane) if plane is not None else -1)
        else:
            self.key = (kind,) + _quantise(self.position)

    def __repr__(self):  # pragma: no cover - debugging aid
        return '<ComponentRef %s %s of %s>' % (
            self.kind, self.key, self.brush.get('name', id(self.brush)))

    def __eq__(self, other):
        return (isinstance(other, ComponentRef) and
                other.brush is self.brush and
                other.kind == self.kind and
                other.key == self.key)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((id(self.brush), self.kind, self.key))


def _quantise(point, scale=8.0):
    """Position rounded to a fraction of a unit — a stable component key."""
    p = _v3(point)
    return (int(round(float(p[0]) * scale)),
            int(round(float(p[1]) * scale)),
            int(round(float(p[2]) * scale)))


# --------------------------------------------------------------------------
# Component enumeration (reads the brush's existing cached geometry)
# --------------------------------------------------------------------------

def brush_shape(brush):
    """Cached convex shape of ``brush`` (angled plane set, or its box)."""
    if not isinstance(brush, dict):
        return None
    shape = bg.get_shape(brush)
    if shape is None or not shape.is_valid:
        return None
    return shape


def brush_points(brush):
    """Corner points of ``brush`` as an (N, 3) array (possibly empty)."""
    shape = brush_shape(brush)
    return np.zeros((0, 3)) if shape is None else shape.verts


def components(brush, mode):
    """Every component of ``brush`` in ``mode`` (empty list in object mode)."""
    shape = brush_shape(brush)
    if shape is None or mode == MODE_OBJECT:
        return []
    verts = shape.verts
    if mode == MODE_VERTEX:
        return [ComponentRef(brush, MODE_VERTEX, (i,), position=verts[i])
                for i in range(len(verts))]
    if mode == MODE_EDGE:
        return [ComponentRef(brush, MODE_EDGE, (i, j),
                             position=(verts[i] + verts[j]) * 0.5)
                for i, j in shape.edges]
    if mode == MODE_FACE:
        out = []
        for face in shape.faces:
            ring = face['indices']
            if len(ring) < 3:
                continue
            out.append(ComponentRef(brush, MODE_FACE, ring,
                                    plane=face['plane'],
                                    position=verts[ring].mean(axis=0)))
        return out
    return []


def resolve_ref(brush, ref):
    """Re-derive ``ref``'s corner indices against the brush's current geometry.

    Returns a fresh :class:`ComponentRef` or ``None`` when the component no
    longer exists (a vertex merged away by an edit, say).  Used to keep a
    component selection meaningful after the geometry it points into changed.
    """
    if ref is None:
        return None
    for candidate in components(brush, ref.kind):
        if candidate.key == ref.key:
            return candidate
    if ref.kind == MODE_FACE:
        return None
    # Vertices/edges are keyed by position; after a drag the moved component
    # sits somewhere new, so fall back to the nearest one within a grid step.
    best, best_d = None, float('inf')
    for candidate in components(brush, ref.kind):
        d = float(np.linalg.norm(candidate.position - ref.position))
        if d < best_d:
            best, best_d = candidate, d
    return best


# --------------------------------------------------------------------------
# Picking helpers
# --------------------------------------------------------------------------

def _point_segment_distance_2d(px, py, ax, ay, bx, by):
    """Distance from (px, py) to the segment (ax, ay)-(bx, by)."""
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ray_point_distance(ray_o, ray_d, point, min_t=0.0):
    """(perpendicular distance, ray parameter) of ``point`` from a ray."""
    rel = point - ray_o
    t = float(rel @ ray_d)
    if t < min_t:
        t = min_t
    closest = ray_o + ray_d * t
    return float(np.linalg.norm(point - closest)), t


def _ray_segment_distance(ray_o, ray_d, a, b, min_t=0.0):
    """(closest distance, ray parameter) between a ray and a segment."""
    u = b - a
    uu = float(u @ u)
    if uu < 1e-12:
        return _ray_point_distance(ray_o, ray_d, a, min_t)
    w0 = ray_o - a
    ud = float(u @ ray_d)
    denom = 1.0 - ud * ud / uu
    if abs(denom) < 1e-9:
        # Parallel: clamp to the segment ends.
        d0, t0 = _ray_point_distance(ray_o, ray_d, a, min_t)
        d1, t1 = _ray_point_distance(ray_o, ray_d, b, min_t)
        return (d0, t0) if d0 <= d1 else (d1, t1)
    t = (float(w0 @ ray_d) * -1.0 + ud * float(w0 @ u) / uu) / denom
    if t < min_t:
        t = min_t
    on_ray = ray_o + ray_d * t
    s = float((on_ray - a) @ u) / uu
    s = 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)
    closest = a + u * s
    # Clamping to the segment may have moved the closest point off the
    # perpendicular, so re-project it back onto the ray for a tight distance.
    t = max(float((closest - ray_o) @ ray_d), min_t)
    return float(np.linalg.norm(closest - (ray_o + ray_d * t))), t


def pick_component_2d(brushes, mode, point2d, axis1, axis2, tolerance):
    """Nearest component of ``brushes`` to a 2D view-plane cursor position.

    ``point2d`` is ``(u, v)`` in the view's two world axes ``axis1``/``axis2``;
    ``tolerance`` is a world-space radius (the caller converts pixels using the
    view's zoom).  Faces are picked with :func:`side_select`, which is what
    makes a face in a 2D view grab-able as the *line* it appears as.

    Only the brushes passed in are tested — callers pass the current selection,
    so this never walks the scene.
    """
    if mode == MODE_OBJECT:
        return None
    px, py = float(point2d[0]), float(point2d[1])
    best, best_d = None, tolerance

    if mode == MODE_FACE:
        depth = ({0, 1, 2} - {axis1, axis2}).pop()
        for brush in brushes:
            shape = brush_shape(brush)
            if shape is None:
                continue
            origin = np.zeros(3)
            origin[axis1] = px
            origin[axis2] = py
            origin[depth] = float(shape.center()[depth])
            direction = np.zeros(3)
            direction[depth] = 1.0
            hits = side_select(shape, origin, direction, tolerance,
                               min_t=-math.inf)
            if not hits:
                continue
            plane_index, distance = hits[0]
            if distance < best_d:
                ring = bg.plane_face_vertex_indices(brush, plane_index)
                if len(ring) < 3:
                    continue
                best_d = distance
                best = ComponentRef(brush, MODE_FACE, ring, plane=plane_index,
                                    position=shape.verts[ring].mean(axis=0))
        return best

    for brush in brushes:
        shape = brush_shape(brush)
        if shape is None:
            continue
        verts = shape.verts
        if mode == MODE_VERTEX:
            for i in range(len(verts)):
                d = math.hypot(float(verts[i][axis1]) - px,
                               float(verts[i][axis2]) - py)
                if d < best_d:
                    best_d = d
                    best = ComponentRef(brush, MODE_VERTEX, (i,),
                                        position=verts[i])
        elif mode == MODE_EDGE:
            for i, j in shape.edges:
                d = _point_segment_distance_2d(
                    px, py,
                    float(verts[i][axis1]), float(verts[i][axis2]),
                    float(verts[j][axis1]), float(verts[j][axis2]))
                if d < best_d:
                    best_d = d
                    best = ComponentRef(brush, MODE_EDGE, (i, j),
                                        position=(verts[i] + verts[j]) * 0.5)
    return best


def pick_component_3d(brushes, mode, ray_o, ray_d, tolerance_at_unit,
                      min_t=0.0):
    """Nearest component of ``brushes`` along a world-space ray.

    ``tolerance_at_unit`` is the pick radius one world unit from the eye; the
    effective radius grows with distance so a handle stays the same size on
    screen.  Faces reuse :func:`engine.brush_geometry.ray_convex_face`, the
    same test Face Mode already uses for texture picking.
    """
    if mode == MODE_OBJECT:
        return None
    ray_o = _v3(ray_o)
    ray_d = _v3(ray_d)
    norm = float(np.linalg.norm(ray_d))
    if norm < 1e-9:
        return None
    ray_d = ray_d / norm

    if mode == MODE_FACE:
        best, best_t = None, float('inf')
        for brush in brushes:
            shape = brush_shape(brush)
            if shape is None:
                continue
            hit = bg.ray_convex_face(shape, ray_o, ray_d)
            if hit is None:
                continue
            t, face = hit
            if t < best_t:
                best_t = t
                ring = face['indices']
                best = ComponentRef(brush, MODE_FACE, ring,
                                    plane=face['plane'],
                                    position=shape.verts[ring].mean(axis=0))
        return best

    best, best_score = None, float('inf')
    for brush in brushes:
        shape = brush_shape(brush)
        if shape is None:
            continue
        verts = shape.verts
        if mode == MODE_VERTEX:
            candidates = [((i,), verts[i], verts[i]) for i in range(len(verts))]
        else:
            candidates = [((i, j), verts[i], verts[j]) for i, j in shape.edges]
        for indices, a, b in candidates:
            if mode == MODE_VERTEX:
                dist, t = _ray_point_distance(ray_o, ray_d, a, min_t)
            else:
                dist, t = _ray_segment_distance(ray_o, ray_d, a, b, min_t)
            limit = tolerance_at_unit * max(t, 1.0)
            if dist > limit:
                continue
            # Prefer the nearest hit to the eye, breaking ties by accuracy.
            score = t + dist
            if score < best_score:
                best_score = score
                position = a if mode == MODE_VERTEX else (a + b) * 0.5
                best = ComponentRef(brush, mode, indices, position=position)
    return best


def side_select(shape, ray_o, ray_d, tolerance, min_t=0.0):
    """Which brush sides the cursor is asking to stretch.

    This is Radiant's side-stretch *concept* expressed in Fio's plane set: a
    side qualifies when the cursor ray runs through the prism formed by all the
    brush's **other** face planes while staying within ``tolerance`` of this
    face's plane.  Clicking just outside (or just inside) one side grabs that
    side; clicking near a corner grabs both sides that meet there, which is how
    a corner drag falls out for free.

    Radiant grabs a side from any distance along the brush's axis.  Fio's 2D
    views use empty-space drags for the marquee and empty-space clicks to
    deselect, so the grab is banded by ``tolerance`` instead — the one
    deliberate divergence from the original behaviour.

    Returns ``[(plane_index, distance), ...]`` sorted nearest first.
    """
    if shape is None or not shape.is_valid:
        return []
    ray_o = _v3(ray_o)
    ray_d = _v3(ray_d)
    normals, offsets = shape.plane_arrays()
    count = len(offsets)
    if count == 0:
        return []

    base = normals @ ray_o - offsets           # signed distance at t = 0
    slope = normals @ ray_d                    # change per unit of t
    hits = []

    for i in range(count):
        t0, t1 = min_t, math.inf
        blocked = False
        for j in range(count):
            if j == i:
                continue
            s = float(slope[j])
            b = float(base[j])
            if abs(s) < 1e-9:
                if b > tolerance:
                    blocked = True           # ray never enters this slab
                    break
                continue
            bound = (tolerance - b) / s
            if s > 0.0:
                t1 = min(t1, bound)
            else:
                t0 = max(t0, bound)
            if t0 > t1:
                blocked = True
                break
        if blocked or t0 > t1:
            continue

        # Signed distance to face i over the surviving interval.
        s_i = float(slope[i])
        b_i = float(base[i])
        ends = []
        for t in (t0, t1):
            if math.isinf(t):
                if abs(s_i) < 1e-9:
                    ends.append(b_i)
                else:
                    ends.append(math.copysign(math.inf, s_i * t))
            else:
                ends.append(b_i + s_i * t)
        nearest = min(ends)
        if nearest < -tolerance:
            # Part of the ray runs inside this face: it enters the solid
            # somewhere else, so this is not the side being pointed at.  (A
            # face perpendicular to a 2D view's screen is rejected here, which
            # is why a top view can only stretch the sides, never the top.)
            continue
        if nearest > tolerance:
            continue        # outside the grab band
        hits.append((i, abs(nearest)))

    hits.sort(key=lambda h: h[1])
    return hits


# --------------------------------------------------------------------------
# Drag operations
# --------------------------------------------------------------------------

class _DragBase:
    """Shared drag bookkeeping.

    A drag snapshots every affected brush once, then recomputes the result from
    that snapshot for the *total* delta on every update.  That gives exactly one
    geometry rebuild per mouse move, no drift from accumulated increments, and a
    trivially correct cancel.
    """

    def __init__(self, label):
        self.label = label
        self.entries = []
        self.changed = False
        self.rejected = False

    def _snapshot(self, brush):
        geo = brush.get('geometry')
        return {
            'brush': brush,
            'had_geometry': geo is not None,
            'planes': [dict(p) for p in (geo or {}).get('planes', [])],
            'pos': list(brush.get('pos', [0, 0, 0])),
            'size': list(brush.get('size', [64, 64, 64])),
        }

    def _restore(self, entry):
        brush = entry['brush']
        if entry['had_geometry']:
            brush['geometry'] = {'planes': [dict(p) for p in entry['planes']]}
        else:
            brush.pop('geometry', None)
        bg._invalidate(brush)
        brush['pos'] = list(entry['pos'])
        brush['size'] = list(entry['size'])

    @property
    def brushes(self):
        return [e['brush'] for e in self.entries]

    def is_empty(self):
        return not self.entries

    def update(self, delta):
        raise NotImplementedError

    def cancel(self):
        """Put every affected brush back exactly as it was at mouse-down."""
        for entry in self.entries:
            self._restore(entry)
        self.changed = False

    def commit(self):
        """Finish the drag, dropping redundant geometry from box brushes.

        A brush that started as a plain box and is *still* an axis-aligned box
        goes back to the compact ``pos``/``size`` form, so side-stretching a box
        never silently promotes it to a plane-set brush (and never costs the
        renderer its fast axis-aligned path).
        """
        for entry in self.entries:
            brush = entry['brush']
            if entry['had_geometry']:
                continue
            if bg.brush_has_geometry(brush) and bg.is_axis_aligned_box(brush):
                brush.pop('geometry', None)
                bg._invalidate(brush)
        return self.changed


class PlaneDrag(_DragBase):
    """Slide whole face planes along their own normals.

    Covers both Radiant behaviours that move a plane rather than its corners:
    the object-mode **side stretch** (one or more sides of the selected
    brushes) and a **face drag** in face mode.  Adjacent faces stay put and the
    brush re-cuts itself against the moved plane, which is what makes a side
    drag feel like resizing.
    """

    def __init__(self, targets, label='side stretch'):
        """``targets`` is an iterable of ``(brush, [plane index, ...])``."""
        _DragBase.__init__(self, label)
        for brush, plane_indices in targets:
            indices = sorted(set(int(i) for i in plane_indices))
            if not indices:
                continue
            # Snapshot *before* promoting a box brush to a plane set, so a
            # cancel restores the compact box form and commit() can tell that
            # the brush only became geometry for the duration of the drag.
            entry = self._snapshot(brush)
            bg.box_to_geometry(brush)
            planes = brush['geometry']['planes']
            entry['targets'] = [
                (i, float(planes[i]['d']), _v3(planes[i]['n']))
                for i in indices if 0 <= i < len(planes)
            ]
            if entry['targets']:
                self.entries.append(entry)

    def update(self, delta):
        delta = _v3(delta)
        changed = False
        for entry in self.entries:
            offsets = {i: d0 + float(n @ delta)
                       for i, d0, n in entry['targets']}
            if bg.offset_brush_planes(entry['brush'], offsets):
                changed = True
            else:
                # Rejected (the move would collapse the brush): leave the last
                # valid state on screen rather than flicking to a bad one.
                self.rejected = True
        if changed:
            self.changed = True
        return changed


class PointDrag(_DragBase):
    """Move brush corners and re-derive the plane set from their hull.

    Covers vertex dragging, edge dragging (both corners of the edge move
    together) and a face *shear* drag (every corner on the face moves, so the
    neighbouring faces tilt to follow instead of staying put).  Convexity is
    guaranteed because the result is a convex hull by construction; a move that
    collapses the brush is rejected and the previous valid shape is kept.
    """

    def __init__(self, targets, label='drag vertex'):
        """``targets`` is an iterable of ``(brush, [corner index, ...])``."""
        _DragBase.__init__(self, label)
        for brush, corner_indices in targets:
            indices = sorted(set(int(i) for i in corner_indices))
            if not indices:
                continue
            points = brush_points(brush)
            if len(points) == 0 or max(indices) >= len(points):
                continue
            entry = self._snapshot(brush)
            entry['points'] = np.array(points, dtype=np.float64, copy=True)
            entry['indices'] = np.array(indices, dtype=np.intp)
            self.entries.append(entry)

    def update(self, delta):
        delta = _v3(delta)
        changed = False
        for entry in self.entries:
            points = entry['points'].copy()
            points[entry['indices']] += delta
            if bg.rebuild_brush_from_points(entry['brush'], points):
                changed = True
            else:
                self.rejected = True
        if changed:
            self.changed = True
        return changed


def begin_component_drag(refs, shear=False):
    """Build the right drag for a set of component references.

    One entry point for every component kind keeps the callers (2D views and
    the 3D viewport) from growing their own per-kind drag code.
    """
    refs = [r for r in refs if r is not None]
    if not refs:
        return None
    kind = refs[0].kind
    grouped = {}
    order = []
    for ref in refs:
        if ref.kind != kind:
            continue
        key = id(ref.brush)
        if key not in grouped:
            grouped[key] = (ref.brush, [])
            order.append(key)
        grouped[key][1].extend(
            ref.indices if kind != MODE_FACE or shear else [ref.plane])
    targets = [grouped[k] for k in order]
    if kind == MODE_FACE and not shear:
        return PlaneDrag(targets, label='drag face')
    label = {MODE_VERTEX: 'drag vertex', MODE_EDGE: 'drag edge',
             MODE_FACE: 'shear face'}.get(kind, 'drag component')
    return PointDrag(targets, label=label)


def begin_side_stretch(brushes, ray_o, ray_d, tolerance, min_t=0.0):
    """Grab whichever sides of ``brushes`` the cursor is pointing at.

    Returns ``(drag, [(brush, plane index), ...])`` or ``(None, [])`` when the
    cursor is not near a side of any of them.
    """
    targets = []
    picked = []
    for brush in brushes:
        shape = brush_shape(brush)
        if shape is None:
            continue
        hits = side_select(shape, ray_o, ray_d, tolerance, min_t=min_t)
        if not hits:
            continue
        planes = [i for i, _ in hits]
        targets.append((brush, planes))
        picked.extend((brush, i) for i in planes)
    if not targets:
        return None, []
    return PlaneDrag(targets, label='side stretch'), picked


# --------------------------------------------------------------------------
# Snapping
# --------------------------------------------------------------------------

def snap_delta(delta, grid, axes=None):
    """Snap a world-space drag delta to the grid.

    ``axes`` limits snapping to the given world axes (a 2D view leaves its
    depth axis alone).  A grid of zero or ``None`` disables snapping, matching
    the editor's grid toggle.
    """
    out = _v3(delta).copy()
    if not grid or grid <= 0:
        return out
    for axis in (axes if axes is not None else (0, 1, 2)):
        out[axis] = round(float(out[axis]) / grid) * grid
    return out


def snap_component_delta(position, delta, grid, axes=None):
    """Snap a drag so the *component* lands on the grid, not just the delta.

    Dragging a vertex should put that vertex on a grid intersection the way
    dragging a brush puts its corner there, so the snap is applied to the
    component's destination and the delta derived back from it.
    """
    position = _v3(position)
    target = position + _v3(delta)
    if not grid or grid <= 0:
        return target - position
    out = target.copy()
    for axis in (axes if axes is not None else (0, 1, 2)):
        out[axis] = round(float(target[axis]) / grid) * grid
    return out - position


# --------------------------------------------------------------------------
# Selection helpers — cycling and Radiant's area selections
# --------------------------------------------------------------------------

def is_hidden(obj):
    if isinstance(obj, dict):
        return bool(obj.get('hidden', False))
    return bool(getattr(obj, 'properties', {}).get('hidden', False))


def is_locked(obj):
    if isinstance(obj, dict):
        return bool(obj.get('lock', False))
    return bool(getattr(obj, 'properties', {}).get('lock', False))


def is_selectable(obj, skip_locked=True):
    """Hidden objects are never selectable; locked ones depend on the setting."""
    if is_hidden(obj):
        return False
    if skip_locked and is_locked(obj):
        return False
    return True


def cycle_pick(candidates, current):
    """Step to the next candidate under the cursor, Radiant's drill-select.

    ``candidates`` must already be in a deterministic order (callers sort by
    hit distance, which is stable for a fixed cursor position).  Repeated
    clicks therefore walk the stack of overlapping brushes in the same order
    every time, and wrap around at the end.
    """
    if not candidates:
        return None
    for i, candidate in enumerate(candidates):
        if candidate is current:
            return candidates[(i + 1) % len(candidates)]
    return candidates[0]


def object_bounds(obj):
    """World AABB of a brush or entity as ``(lo, hi)`` float arrays.

    Brushes keep ``pos``/``size`` in sync with their geometry (see
    ``brush_geometry.sync_brush_bounds``), so this is exact for angled brushes
    too without touching their plane sets.
    """
    if isinstance(obj, dict):
        pos = _v3(obj.get('pos', [0, 0, 0]))
        half = _v3(obj.get('size', [0, 0, 0])) * 0.5
        return pos - half, pos + half
    p = _v3(obj.pos)
    return p.copy(), p.copy()


def _candidates(objects, exclude, skip_locked):
    for obj in objects:
        if obj is exclude:
            continue
        if not is_selectable(obj, skip_locked):
            continue
        yield obj


def select_touching(objects, lo, hi, exclude=None, skip_locked=True, eps=1.0):
    """Everything whose bounds overlap the region — Radiant's Select Touching."""
    lo, hi = _v3(lo), _v3(hi)
    out = []
    for obj in _candidates(objects, exclude, skip_locked):
        b_lo, b_hi = object_bounds(obj)
        if np.all(b_lo <= hi + eps) and np.all(b_hi >= lo - eps):
            out.append(obj)
    return out


def select_inside(objects, lo, hi, exclude=None, skip_locked=True):
    """Everything fully contained in the region — Radiant's Select Inside."""
    lo, hi = _v3(lo), _v3(hi)
    out = []
    for obj in _candidates(objects, exclude, skip_locked):
        b_lo, b_hi = object_bounds(obj)
        if np.all(b_lo >= lo) and np.all(b_hi <= hi):
            out.append(obj)
    return out


def select_partial_tall(objects, lo, hi, axis1, axis2, exclude=None,
                        skip_locked=True):
    """Anything overlapping the region in the view plane, at any depth.

    Radiant's Select Partial Tall: the marker brush's footprint is treated as
    infinitely tall along the view's depth axis, so it catches everything that
    crosses that column.
    """
    lo, hi = _v3(lo), _v3(hi)
    out = []
    for obj in _candidates(objects, exclude, skip_locked):
        b_lo, b_hi = object_bounds(obj)
        if (b_lo[axis1] > hi[axis1] or b_hi[axis1] < lo[axis1] or
                b_lo[axis2] > hi[axis2] or b_hi[axis2] < lo[axis2]):
            continue
        out.append(obj)
    return out


def select_complete_tall(objects, lo, hi, axis1, axis2, exclude=None,
                         skip_locked=True):
    """Anything wholly inside the region's column — Radiant's Complete Tall."""
    lo, hi = _v3(lo), _v3(hi)
    out = []
    for obj in _candidates(objects, exclude, skip_locked):
        b_lo, b_hi = object_bounds(obj)
        if (b_hi[axis1] > hi[axis1] or b_lo[axis1] < lo[axis1] or
                b_hi[axis2] > hi[axis2] or b_lo[axis2] < lo[axis2]):
            continue
        out.append(obj)
    return out


# --------------------------------------------------------------------------
# Controller — the shared state the views read and write
# --------------------------------------------------------------------------

class ComponentController:
    """Component mode, hover, selection and the in-flight drag.

    One instance lives on the main window; the 2D views and the 3D viewport
    both drive it, so "click geometry, drag geometry" means the same thing in
    either view and there is one place that knows what mode the editor is in.

    :attr:`version` changes whenever anything a highlight overlay would draw
    changes.  Renderers cache their overlay buffers against it and skip the
    rebuild entirely while it is unchanged, so hovering never costs the scene a
    geometry rebuild.
    """

    def __init__(self):
        self.mode = MODE_OBJECT
        self.hover = None
        self.selection = []
        self.drag = None
        self.version = 0
        self._overlay_cache = None
        self._overlay_version = -1

    # -- mode -------------------------------------------------------------
    def set_mode(self, mode):
        """Switch component mode; returns ``True`` when it actually changed."""
        if mode not in COMPONENT_MODES or mode == self.mode:
            return False
        self.mode = mode
        self.hover = None
        self.selection = []
        self.cancel_drag()
        self.invalidate()
        return True

    def is_component_mode(self):
        return self.mode != MODE_OBJECT

    # -- invalidation -----------------------------------------------------
    def invalidate(self):
        """Mark the overlay stale.  Cheap — just bumps a counter."""
        self.version += 1

    # -- hover / selection -------------------------------------------------
    def set_hover(self, ref):
        if ref == self.hover:
            return False
        self.hover = ref
        self.invalidate()
        return True

    def set_selection(self, refs):
        self.selection = [r for r in refs if r is not None]
        self.invalidate()

    def toggle(self, ref):
        """Add/remove one component from the selection (shift-click)."""
        if ref is None:
            return
        for i, existing in enumerate(self.selection):
            if existing == ref:
                del self.selection[i]
                self.invalidate()
                return
        self.selection.append(ref)
        self.invalidate()

    def clear(self):
        if self.hover is None and not self.selection:
            return
        self.hover = None
        self.selection = []
        self.invalidate()

    def prune(self, live_brushes):
        """Drop references to brushes that are gone, and re-resolve the rest."""
        live = {id(b) for b in live_brushes}
        refs = []
        for ref in self.selection:
            if id(ref.brush) not in live:
                continue
            resolved = resolve_ref(ref.brush, ref)
            if resolved is not None:
                refs.append(resolved)
        if len(refs) != len(self.selection):
            self.invalidate()
        self.selection = refs
        if self.hover is not None and id(self.hover.brush) not in live:
            self.hover = None
            self.invalidate()

    # -- drags -------------------------------------------------------------
    def begin_drag(self, drag):
        self.drag = drag
        return drag is not None and not drag.is_empty()

    def update_drag(self, delta):
        if self.drag is None:
            return False
        changed = self.drag.update(delta)
        if changed:
            self.invalidate()
        return changed

    def commit_drag(self):
        """Finish the drag and return whether anything actually moved."""
        if self.drag is None:
            return False
        changed = self.drag.commit()
        brushes = self.drag.brushes
        self.drag = None
        self.selection = [r for r in
                          (resolve_ref(ref.brush, ref) for ref in self.selection)
                          if r is not None]
        self.hover = None
        self.invalidate()
        for brush in brushes:
            bg.sync_brush_bounds(brush)
        return changed

    def cancel_drag(self):
        if self.drag is None:
            return False
        self.drag.cancel()
        self.drag = None
        self.invalidate()
        return True

    # -- overlay -----------------------------------------------------------
    def overlay(self, brushes):
        """Handle positions for the component overlay, cached by version.

        Returns a dict with ``points`` (N, 3), ``lines`` (M, 2, 3) and the
        highlighted subsets, all ``float32`` and ready to hand to a renderer.
        Rebuilt only when :attr:`version` moved, so a hover that changes
        nothing costs one integer comparison.
        """
        if (self._overlay_cache is not None and
                self._overlay_version == self.version):
            return self._overlay_cache

        points = []
        lines = []
        hot_points = []
        hot_lines = []
        selected_keys = {r.key for r in self.selection}
        hover_key = self.hover.key if self.hover is not None else None

        if self.is_component_mode():
            for brush in brushes:
                shape = brush_shape(brush)
                if shape is None:
                    continue
                verts = shape.verts
                for ref in components(brush, self.mode):
                    hot = (ref.key in selected_keys or ref.key == hover_key)
                    if self.mode == MODE_VERTEX:
                        (hot_points if hot else points).append(ref.position)
                    elif self.mode == MODE_EDGE:
                        i, j = ref.indices
                        (hot_lines if hot else lines).append(
                            (verts[i], verts[j]))
                    else:
                        (hot_points if hot else points).append(ref.position)
                        ring = list(ref.indices)
                        for k in range(len(ring)):
                            pair = (verts[ring[k]],
                                    verts[ring[(k + 1) % len(ring)]])
                            if hot:
                                hot_lines.append(pair)

        def _pts(seq):
            return (np.asarray(seq, dtype=np.float32) if seq
                    else np.zeros((0, 3), dtype=np.float32))

        def _lns(seq):
            return (np.asarray(seq, dtype=np.float32) if seq
                    else np.zeros((0, 2, 3), dtype=np.float32))

        self._overlay_cache = {
            'points': _pts(points),
            'hot_points': _pts(hot_points),
            'lines': _lns(lines),
            'hot_lines': _lns(hot_lines),
        }
        self._overlay_version = self.version
        return self._overlay_cache
