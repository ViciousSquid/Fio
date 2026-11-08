"""Fio's brush semantics -- an executable statement, kept in step by hand.

Pinned at Stage 0 of the unified brush model (``docs/brush-model-proposal.md``)
and changed only by a stage that changes behaviour on purpose; Stage 1 (one
face record), Stage 2a (derived bounds) and Stage 2b (runtime transforms
owned by a runtime source, ``docs/brush-model-stage2.md``) are the current
statement. A brush dict has one of two encodings:

* **box** (no ``geometry.planes``): ``pos``/``size`` are the geometry. Each
  face's material lives in the per-tag dicts -- ``textures``, ``uv_scale``,
  ``uv_angle``, ``uv_shift``, ``uv_natural`` -- plus the brush-wide
  ``texture_tiling``.
* **convex** (``geometry.planes``): the plane set is the geometry, and each
  face's material lives on its plane, tagged and cut faces alike. A legacy
  brush may still carry per-tag copies for its tagged faces; the Stage 1
  precedence settles them (restated independently in
  :func:`_convex_material`). Its bound is the planes' own AABB (extents
  floored at 1e-3); the ``pos``/``size`` it stores are a copy
  (:func:`stored_bound`) nothing reads for a bound, re-derived at load.
  Since Stage 2d the planes are stored in the brush's local frame, centred
  on their own bounds, and ``pos`` places them; a shipped map (version 3)
  stores world-space planes, so a record read straight from a file is
  marked (:data:`WORLD_PLANES`) and :func:`migrated` localises it as the
  loader does.

Both encodings are drawn through one transform -- the bound (:func:`bound`)
and the spin (:func:`transform`) -- and every bound-reader (culling,
collision rows, spatial grid, Big World, picking) reads the same bound,
turned by the spin. A mover's or door's displacement and spin are its
*runtime transform*, owned by a runtime source (the mover table in play, the
editor's preview, a test's driver) and read here as an input, through
``engine.brush_placement.runtime_of``; nothing play does is written into
the dict, whose ``pos`` is the authored placement. A convex mesh is
expressed relative to its planes' AABB (its *anchor*) and drawn back through
the bound, so the planes are drawn where they are, displaced only by a
runtime transform.

This module computes, from a brush dict alone and without GL: the pass that
draws the brush, the faces it draws (world-space polygon + resolved
material), the bound the bound-readers use, and what a brush's data carries
that its one record has to settle. The tests anchor it to the engine -- the
RenderTable columns the box pass consumes, the convex records the renderer
reads, the frozen copy of the convex path that the GL draw oracle proves
against the GPU, and the GPU state itself -- so it states the engine's
behaviour rather than an opinion of it. Do not "fix" it: a deliberate
behaviour change updates it in the same commit, and the snapshot
(``tests/brush_model``) shows the change.
"""

import copy
import glob
import hashlib
import json
import os
from collections import Counter

import numpy as np

from engine import brush_geometry as bg

#: Box face order and the axis/sign of each face's outward normal; the same
#: order as the cube VAO (``render_table.CUBE_FACE_KEYS``).
BOX_FACES = (('south', 2, -1), ('north', 2, 1), ('west', 0, -1),
             ('east', 0, 1), ('down', 1, -1), ('top', 1, 1))
DEFAULT_TEXTURE = 'default.png'

#: Texture names that do not make a brush "textured" (``_brush_class_bits``).
UNTEXTURED = (DEFAULT_TEXTURE, 'caulk.jpg')

#: Keys play wrote into the authored dict before Stage 2b, and the map
#: serialiser kept: the mover's restore point and its spin (``_rot_angle``,
#: mirrored to ``rotation_yaw``). Shipped map files still carry them; loading
#: drops them, and nothing writes them.
RUNTIME_KEYS = ('original_pos', 'rotation_yaw', '_rot_angle')

#: The per-tag material dicts and the plane field each one shadows on a
#: legacy tagged convex face.
TAG_FIELDS = (('textures', 'texture'), ('uv_scale', 'uv_scale'),
              ('uv_shift', 'uv_shift'), ('uv_angle', 'uv_angle'))


#: Set on a record read straight from a shipped file (:func:`shipped_maps`):
#: its planes are in world space, as every map before version 5 stores them.
WORLD_PLANES = '_shipped_world_planes'


def world_convex(brush):
    """The brush's solid in world space at its authored placement: a file
    record's planes as stored, a live (version 5) brush's local planes
    carried by its ``pos`` (``engine.brush_geometry.get_convex``)."""
    if brush.get(WORLD_PLANES):
        return bg.ConvexGeometry(brush['geometry']['planes'])
    return bg.get_convex(brush)


def encoding(brush):
    """``'box'``, ``'convex'``, or ``'convex-invalid'`` (planes enclosing no
    volume: the renderer draws nothing for its textured faces)."""
    if not bg.brush_has_geometry(brush):
        return 'box'
    convex = world_convex(brush)
    return 'convex' if convex is not None and convex.is_valid else 'convex-invalid'


def stored_bound(brush):
    """``(centre, size)`` as the dict stores it: ``pos``/``size``."""
    pos = brush.get('pos') or (0.0, 0.0, 0.0)
    size = brush.get('size') or (64.0, 64.0, 64.0)
    return (tuple(float(v) for v in pos), tuple(float(v) for v in size))


def geometry_rest(brush):
    """``(centre, size)`` of a valid convex brush's planes' AABB, extents
    floored at 1e-3; ``None`` for anything else."""
    if encoding(brush) != 'convex':
        return None
    lo, hi = world_convex(brush).bounds
    centre = (lo + hi) * 0.5
    extent = hi - lo
    return (tuple(float(v) for v in centre),
            tuple(max(float(v), 1e-3) for v in extent))


def local_rest(brush):
    """``(centre, size)`` of a valid convex brush's planes *as stored* -- its
    local frame since Stage 2d, where the renderer builds its mesh (the
    mesh's anchor) -- extents floored at 1e-3; ``None`` for anything else."""
    if encoding(brush) != 'convex':
        return None
    lo, hi = bg.ConvexGeometry(brush['geometry']['planes']).bounds
    centre = (lo + hi) * 0.5
    extent = hi - lo
    return (tuple(float(v) for v in centre),
            tuple(max(float(v), 1e-3) for v in extent))


def _runtime(brush):
    """The brush's runtime transform -- an input, like the dict: its centre
    now and spin, ``(cx, cy, cz, ax, ay, az, angle)``, or ``None``."""
    from engine.brush_placement import runtime_of
    return runtime_of(brush)


def bound(brush):
    """``(centre, size)`` the bound-readers use: a box's ``pos``/``size``,
    a convex brush's planes' AABB (since Stage 2d: its stored, local planes'
    box placed by ``pos``), an invalid plane set's ``pos``/``size`` --
    centred where the runtime transform has carried it."""
    rest = geometry_rest(brush)
    if rest is None:
        centre, size = stored_bound(brush)
    elif brush.get(WORLD_PLANES):
        centre, size = rest                     # a file record: world planes
    else:
        # Stage 2d: the stored planes' own box (local), placed by pos.
        local_centre, size = local_rest(brush)
        pos = stored_bound(brush)[0]
        centre = tuple(pos[i] + local_centre[i] for i in range(3))
    runtime = _runtime(brush)
    if runtime is not None:
        centre = tuple(float(v) for v in runtime[:3])
    return centre, size



def draw_pass(brush):
    """``'textured'`` or ``'solid'``: which opaque pass draws the brush.

    Textured if any face names a real texture -- a box's per-tag
    ``textures``, a convex brush's planes (its faces' one record);
    otherwise the lit pass draws every face in the brush colour.
    """
    if bg.brush_has_geometry(brush):
        names = [plane.get('texture') for plane in brush['geometry']['planes']]
    else:
        names = list((brush.get('textures') or {}).values())
    for name in names:
        if name and name not in UNTEXTURED:
            return 'textured'
    return 'solid'


def transform(brush):
    """``(centre, size, axis, angle_degrees)`` both encodings are drawn
    through; *axis* is ``None`` for an unrotated brush."""
    centre, size = bound(brush)
    runtime = _runtime(brush)
    angle = float(runtime[6]) if runtime is not None else 0.0
    axis = tuple(float(v) for v in runtime[3:6]) if angle else None
    return centre, size, axis, angle


def posed(brush, points):
    """*points* (world space, as :func:`render_faces` gives them) where the
    renderer draws them: a convex mover's planes carried from their rest to
    its bound, then rotated by the spin about the bound's centre."""
    centre, _size, axis, angle = transform(brush)
    points = np.asarray(points, dtype=np.float64)
    rest = geometry_rest(brush)
    if rest is not None:
        points = points + (np.asarray(centre) - np.asarray(rest[0]))
    if axis is None:
        return points
    axis = np.asarray(axis, dtype=np.float64)
    length = float(np.linalg.norm(axis))
    if length <= 0.001:
        return points
    ux, uy, uz = axis / length
    a = np.radians(angle)
    c, s = np.cos(a), np.sin(a)
    k = 1.0 - c
    rotation = np.array([
        [c + ux * ux * k, ux * uy * k - uz * s, ux * uz * k + uy * s],
        [uy * ux * k + uz * s, c + uy * uy * k, uy * uz * k - ux * s],
        [uz * ux * k - uy * s, uz * uy * k + ux * s, c + uz * uz * k]])
    centre = np.asarray(centre)
    return (rotation @ (points - centre).T).T + centre


def world_aabb(brush):
    """``(lo, hi)`` every bound-reader uses (Stage 2a): the AABB of the
    bound's eight corners where the renderer draws them -- turned about the
    bound's centre by the spin."""
    centre, size = (np.asarray(v, dtype=np.float64) for v in bound(brush))
    corners = np.array([[sx, sy, sz] for sx in (-0.5, 0.5)
                        for sy in (-0.5, 0.5) for sz in (-0.5, 0.5)]) * size + centre
    rest = geometry_rest(brush)
    if rest is not None:            # posed() carries a convex brush's planes
        corners = corners - (centre - np.asarray(rest[0]))
    turned = posed(brush, corners)
    return turned.min(axis=0), turned.max(axis=0)


def _box_material(brush, tag):
    scales = brush.get('uv_scale') or {}
    shifts = brush.get('uv_shift') or {}
    angles = brush.get('uv_angle') or {}
    scale = scales.get(tag)
    natural = bool(bg.face_uses_natural_scale(brush, tag))
    if scale is None and brush.get('texture_tiling', False):
        natural = True                       # brush-wide tiling: as the renderer
    shift = shifts.get(tag) or (0.0, 0.0)
    return {
        'texture': (brush.get('textures') or {}).get(tag, DEFAULT_TEXTURE),
        'uv_scale': None if scale is None else (float(scale[0]), float(scale[1])),
        'uv_angle': float(angles.get(tag, 0.0)),
        'uv_shift': (float(shift[0]), float(shift[1])),
        'natural': natural,
    }


def _tag_entry(brush, key, tag):
    entries = brush.get(key)
    if not tag or not isinstance(entries, dict):
        return None
    return entries.get(tag)


def _convex_material(brush, face, plane):
    """A convex face's material at run time: its plane alone (a per-tag dict
    is never consulted for a convex face), with brush-wide tiling as for a
    box."""
    scale = plane.get('uv_scale')
    shift = plane.get('uv_shift') or (0.0, 0.0)
    natural = bool(plane.get('uv_natural'))
    if scale is None and brush.get('texture_tiling', False):
        natural = True
    return {
        'texture': plane.get('texture') or DEFAULT_TEXTURE,
        'uv_scale': None if scale is None else (float(scale[0]), float(scale[1])),
        'uv_angle': float(plane.get('uv_angle', 0.0)),
        'uv_shift': (float(shift[0]), float(shift[1])),
        'natural': natural,
    }


#: The per-tag dict each plane field comes from in a legacy brush.
_LEGACY_KEYS = (('texture', 'textures'), ('uv_scale', 'uv_scale'),
                ('uv_shift', 'uv_shift'), ('uv_angle', 'uv_angle'),
                ('uv_natural', 'uv_natural'))


def migrated(brush):
    """*brush* as the editor loads it from a map saved before the canonical
    layout -- restated here, independently of ``engine.brush_faces``.

    For each tagged face of a convex brush: texture from the plane; scale,
    shift and angle from the per-tag entry when there is one, else the
    plane; Natural if either home sets it. The result goes on the plane and
    the per-tag entries of tags the planes carry are dropped. Boxes and the
    input are left untouched.
    """
    out = copy.deepcopy(brush)
    if not bg.brush_has_geometry(out):
        return out
    planes = out['geometry']['planes']
    tags = {plane.get('face') for plane in planes if plane.get('face')}
    for plane in planes:
        tag = plane.get('face')
        if not tag:
            continue
        for field, key in _LEGACY_KEYS[1:4]:
            entry = _tag_entry(out, key, tag)
            if entry is not None:
                plane[field] = (float(entry) if field == 'uv_angle'
                                else [float(entry[0]), float(entry[1])])
        if _tag_entry(out, 'uv_natural', tag):
            plane['uv_natural'] = True
    for _field, key in _LEGACY_KEYS:
        entries = out.get(key)
        if isinstance(entries, dict):
            kept = {k: v for k, v in entries.items() if k not in tags}
            if kept:
                out[key] = kept
            else:
                out.pop(key)
    bg.invalidate_geometry_cache(out)
    return out


def localised(brush):
    """A copy of a file record (:data:`WORLD_PLANES`) in the frame the editor
    holds it in since Stage 2d: its world-space planes re-expressed about
    their own centre, which becomes ``pos`` -- the loader's own step
    (``engine.brush_placement.localise_legacy``), so the result is exactly
    what a load holds. Materials are left as stored. Any other brush is
    copied as it is."""
    out = copy.deepcopy(brush)
    if out.pop(WORLD_PLANES, None):
        from engine.brush_placement import localise_legacy
        localise_legacy(out)
    return out


def loaded(brush):
    """*brush* as the editor holds it after loading a shipped map:
    :func:`migrated` (materials) then :func:`localised` (frame)."""
    return localised(migrated(brush))


def render_faces(brush):
    """The faces the renderer draws, before the run-time transform.

    A list of ``(tag, polygon, material)``: *polygon* is the world-space
    corner ring (a box's from its bound, a convex brush's from its planes --
    the convex mesh is stored relative to the planes' AABB and drawn back
    through the bound, so they cancel), *material* the face state the
    textured pass draws it with. A brush :func:`draw_pass` sends to the
    solid pass is drawn in its colour instead. A convex mover's
    displacement and every spin are applied on top (:func:`posed`).
    """
    kind = encoding(brush)
    if kind == 'convex-invalid':
        return []
    if kind == 'box':
        centre, size = (np.asarray(v) for v in bound(brush))
        half = size * 0.5
        faces = []
        for tag, axis, sign in BOX_FACES:
            others = [a for a in range(3) if a != axis]
            ring = []
            for du, dv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                point = centre.copy()
                point[axis] += sign * half[axis]
                point[others[0]] += du * half[others[0]]
                point[others[1]] += dv * half[others[1]]
                ring.append(tuple(float(c) for c in point))
            faces.append((tag, tuple(ring), _box_material(brush, tag)))
        return faces
    convex = world_convex(brush)
    return [(face.get('face'),
             tuple(tuple(float(c) for c in convex.verts[i]) for i in face['indices']),
             _convex_material(brush, face, convex.planes[face['plane']]))
            for face in convex.faces]


def geometry_bound(brush):
    """``(centre, size)`` of what the renderer draws (``None`` if nothing)."""
    faces = render_faces(brush)
    if not faces:
        return None
    points = np.array([p for _tag, ring, _m in faces for p in ring])
    lo, hi = points.min(axis=0), points.max(axis=0)
    return tuple((lo + hi) * 0.5), tuple(hi - lo)


def _plane_value(plane, field):
    """A plane's own value of one per-tag field, or ``None``."""
    value = plane.get(field)
    if value is None:
        return None
    if field in ('texture', 'uv_natural'):
        return value or None
    if field == 'uv_angle':
        return float(value)
    return (float(value[0]), float(value[1]))


#: What a face draws when its record has no value for a field.
_DRAWN_DEFAULT = {'texture': DEFAULT_TEXTURE, 'uv_scale': (1.0, 1.0),
                  'uv_shift': (0.0, 0.0), 'uv_angle': 0.0}


def _tag_value(entry, field):
    if field in ('texture', 'uv_natural'):
        return entry or None
    if field == 'uv_angle':
        return float(entry)
    return (float(entry[0]), float(entry[1]))


def inconsistencies(brush):
    """What this brush's data carries that its one record has to settle, as
    ``{kind: detail}``.

    * ``runtime_keys``: play's keys (:data:`RUNTIME_KEYS`) in the authored
      dict.
    * ``bound_drift``: a convex brush whose stored copy of its bounds is not
      its planes' AABB (bit for bit); the detail is the largest difference.
      In a file record the copy is ``pos``/``size``; since Stage 2d ``pos``
      is the placement and only ``size`` is a copy (of the local planes'
      extents). Nothing reads the copy for a bound since Stage 2a, and
      loading re-derives it.
    * ``resolved_from_tag``: a legacy tagged convex face whose per-tag entry
      differs from its plane and is the authoritative value (scale, shift,
      angle: what the Surface Inspector showed); the face draws it, where it
      drew the plane's before Stage 1. The detail lists ``'tag.field'``.
    * ``discarded_tag_value``: a legacy per-tag entry that differs from the
      plane and is not authoritative (texture: the plane draws and always
      did); load normalisation drops it.
    """
    found = {}
    runtime = [key for key in RUNTIME_KEYS if brush.get(key) is not None]
    if runtime:
        found['runtime_keys'] = runtime
    if encoding(brush) != 'convex':
        return found
    pos, size = (np.asarray(v, dtype=np.float64) for v in stored_bound(brush))
    if brush.get(WORLD_PLANES):
        # A file record: pos/size are a copy of the world planes' box.
        lo, hi = world_convex(brush).bounds
        centre, extent = (lo + hi) * 0.5, hi - lo
        if centre.tobytes() != pos.tobytes() or extent.tobytes() != size.tobytes():
            found['bound_drift'] = float(max(np.abs(centre - pos).max(),
                                             np.abs(extent - size).max()))
    else:
        # Stage 2d: pos is the placement, not a copy; size copies the
        # stored (local) planes' extents.
        lo, hi = bg.ConvexGeometry(brush['geometry']['planes']).bounds
        extent = hi - lo
        if extent.tobytes() != size.tobytes():
            found['bound_drift'] = float(np.abs(extent - size).max())
    resolved, discarded = [], []
    for plane in brush['geometry']['planes']:
        tag = plane.get('face')
        if not tag:
            continue
        for key, field in TAG_FIELDS:
            entry = _tag_entry(brush, key, tag)
            if entry is None:
                continue
            # Compared as drawn: a missing value is the default it stands for.
            per_tag = _tag_value(entry, field)
            own = _plane_value(plane, field)
            if own is None:
                own = _DRAWN_DEFAULT[field]
            if per_tag == own:
                continue
            (discarded if field == 'texture' else resolved).append('%s.%s' % (tag, field))
        # Natural is set by either home and never cleared by one: nothing to
        # settle.
    if resolved:
        found['resolved_from_tag'] = sorted(resolved)
    if discarded:
        found['discarded_tag_value'] = sorted(discarded)
    return found


def _rounded(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in sorted(value.items())}
    return value


def digest(brushes):
    """A stable digest of every brush's encoding, pass, bound and drawn faces.

    Rounded to 1e-4 world units so it pins behaviour, not the last bits of a
    plane solve on a different machine; a change of any visible size alters
    it.
    """
    record = []
    for brush in brushes:
        record.append([brush.get('id'), encoding(brush), draw_pass(brush),
                       _rounded(bound(brush)),
                       [_rounded([tag, ring, material])
                        for tag, ring, material in render_faces(brush)]])
    return hashlib.sha256(json.dumps(record).encode()).hexdigest()


#: Bound drift at or below this is the plane solver's last bits, not a
#: second opinion of where the brush is; the snapshot does not pin it.
DRIFT_NOISE = 1e-6


def map_snapshot(brushes):
    """What :func:`snapshot` records for one map's brushes, as stored in the
    file: the inventory of what the file's data carries, and the digest of
    what the editor draws once it has loaded (and, for a legacy file,
    migrated) them."""
    inventory = {}
    for index, brush in enumerate(brushes):
        found = inconsistencies(brush)
        drift = found.pop('bound_drift', None)
        if drift is not None and drift > DRIFT_NOISE:
            found['bound_drift'] = float('%.3g' % drift)
        if found:
            inventory[str(brush.get('id') or '#%d' % index)] = found
    counts = Counter(encoding(brush) for brush in brushes)
    return json.loads(json.dumps({
        'brushes': len(brushes),
        'encodings': dict(sorted(counts.items())),
        'digest': digest([migrated(brush) for brush in brushes]),
        'inconsistencies': dict(sorted(inventory.items())),
    }))


def shipped_maps(root):
    """``{file name: brushes}`` for every map in ``maps/`` that has brushes,
    each brush a shallow copy of the file's record (every shipped map is a
    legacy, version 3 map: :func:`migrated` gives it as the editor loads it)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(root, 'maps', '*.json'))):
        with open(path) as handle:
            data = json.load(handle)
        brushes = [dict(brush, **{WORLD_PLANES: True}) if bg.brush_has_geometry(brush)
                   else dict(brush) for brush in data.get('brushes') or []]
        if brushes:
            out[os.path.basename(path)] = brushes
    return out


def snapshot(root):
    """The semantics snapshot of every shipped map (the fixture's content)."""
    return {name: map_snapshot(brushes)
            for name, brushes in shipped_maps(root).items()}
