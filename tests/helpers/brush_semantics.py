"""Fio's brush semantics as they are today -- an executable statement.

Stage 0 of the unified brush model (``docs/brush-model-proposal.md``) changes
no behaviour; it pins what a brush *means* now, so every later stage is a
deliberate, reviewed change rather than a silent one. A brush dict is read
under two encodings that overlap:

* **box** (no ``geometry.planes``): ``pos``/``size`` are the geometry. Face
  material lives in per-tag dicts -- ``textures``, ``uv_scale``, ``uv_angle``,
  ``uv_shift``, ``uv_natural`` -- plus the brush-wide ``texture_tiling``.
* **convex** (``geometry.planes``): the plane set is the geometry, and each
  plane carries its own face material. ``pos``/``size`` are a *derived bound*
  (``brush_geometry.sync_brush_bounds``); the renderer draws the planes and
  the bound-readers (collision AABBs, spatial grid, culling, picking) read
  ``pos``/``size``.

Both encodings are drawn through one transform: ``pos``/``size`` and
``_rot_angle``/``rot_axis`` (:func:`transform`). Movers write ``pos`` and
``_rot_angle`` into the authored dict every tick; ``original_pos`` restores
it. A convex mesh is expressed relative to the ``pos``/``size`` it had when
its geometry was resolved (its *anchor*), so for a freshly loaded brush the
two cancel and the planes are drawn where they are.

This module computes, from a brush dict alone and without GL: the pass that
draws the brush, the faces it draws (world-space polygon + resolved
material), the bound the bound-readers use, and the inconsistencies a brush
carries. The tests anchor
it to the engine -- the RenderTable columns the box pass consumes, the frozen
copy of the convex path that the GL draw oracle proves against the GPU, and
the GPU state itself -- so it states the engine's behaviour rather than an
opinion of it. Do not "fix" it: a deliberate behaviour change updates it in
the same commit, and the snapshot (``tests/brush_model``) shows the change.
"""

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

#: Keys play writes into the authored dict that the map serialiser keeps:
#: the mover's restore point and its spin (``_rot_angle``, mirrored to
#: ``rotation_yaw``).
RUNTIME_KEYS = ('original_pos', 'rotation_yaw', '_rot_angle')

#: The per-tag material dicts and the plane field each one shadows on a
#: tagged convex face. The plane is what renders.
TAG_FIELDS = (('textures', 'texture'), ('uv_scale', 'uv_scale'),
              ('uv_shift', 'uv_shift'), ('uv_angle', 'uv_angle'))


def encoding(brush):
    """``'box'``, ``'convex'``, or ``'convex-invalid'`` (planes enclosing no
    volume: the renderer draws nothing for its textured faces)."""
    if not bg.brush_has_geometry(brush):
        return 'box'
    convex = bg.get_convex(brush)
    return 'convex' if convex is not None and convex.is_valid else 'convex-invalid'


def bound(brush):
    """``(centre, size)`` as the bound-readers use it: ``pos``/``size``."""
    pos = brush.get('pos') or (0.0, 0.0, 0.0)
    size = brush.get('size') or (64.0, 64.0, 64.0)
    return (tuple(float(v) for v in pos), tuple(float(v) for v in size))



def draw_pass(brush):
    """``'textured'`` or ``'solid'``: which opaque pass draws the brush.

    The engine's rule (``render_table._brush_class_bits``), for both
    encodings: textured if any *per-tag* ``textures`` entry names a real
    texture; otherwise the lit pass draws every face in the brush colour,
    whatever the faces' own textures are. A convex brush is classified from
    the per-tag copy too, not from its planes.
    """
    for name in (brush.get('textures') or {}).values():
        if name and name not in UNTEXTURED:
            return 'textured'
    return 'solid'


def transform(brush):
    """``(centre, size, axis, angle_degrees)`` both encodings are drawn
    through; *axis* is ``None`` for an unrotated brush."""
    centre, size = bound(brush)
    angle = float(brush.get('_rot_angle') or 0.0)
    axis = (tuple(float(v) for v in (brush.get('rot_axis') or (0.0, 1.0, 0.0)))
            if angle else None)
    return centre, size, axis, angle


def posed(brush, points):
    """*points* (world space, as :func:`render_faces` gives them) where the
    renderer draws them: rotated by ``_rot_angle`` about ``pos`` -- the
    bound's centre, for a convex brush too, not its geometry's."""
    centre, _size, axis, angle = transform(brush)
    points = np.asarray(points, dtype=np.float64)
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


def _convex_material(brush, face, plane):
    scale = face.get('uv_scale') or plane.get('uv_scale')
    shift = plane.get('uv_shift', (0.0, 0.0)) or (0.0, 0.0)
    # The renderer passes the convex *face* dict as ``plane`` here, and a face
    # dict has no ``uv_natural``: only the brush's per-tag flag counts.
    natural = bool(bg.face_uses_natural_scale(brush, face.get('face'), face))
    return {
        'texture': face.get('texture') or DEFAULT_TEXTURE,
        'uv_scale': None if scale is None else (float(scale[0]), float(scale[1])),
        'uv_angle': float(plane.get('uv_angle', 0.0)),
        'uv_shift': (float(shift[0]), float(shift[1])),
        'natural': natural,
    }


def render_faces(brush):
    """The faces the renderer draws, before the run-time transform.

    A list of ``(tag, polygon, material)``: *polygon* is the world-space
    corner ring (a box's from ``pos``/``size``, a convex brush's from its
    planes -- the convex mesh is stored relative to ``pos``/``size`` and
    drawn back through them, so they cancel), *material* the face state the
    textured pass draws it with. A brush :func:`draw_pass` sends to the
    solid pass is drawn in its colour instead. ``_rot_angle``/``rot_axis``
    are applied on top (:func:`posed`).
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
    convex = bg.get_convex(brush)
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
    """A tagged convex face's rendered value of one per-tag field."""
    if field == 'texture':
        return plane.get('texture') or DEFAULT_TEXTURE
    if field == 'uv_angle':
        return float(plane.get('uv_angle', 0.0))
    value = plane.get(field)
    if field == 'uv_shift':
        value = value or (0.0, 0.0)
    return None if value is None else (float(value[0]), float(value[1]))


def _tag_value(entry, field):
    if field in ('texture', 'uv_angle'):
        return entry if field == 'texture' else float(entry)
    return (float(entry[0]), float(entry[1]))


def inconsistencies(brush):
    """What this brush's two readings disagree on, as ``{kind: detail}``.

    * ``runtime_keys``: play's keys (:data:`RUNTIME_KEYS`) in the authored
      dict.
    * ``bound_drift``: a convex brush whose ``pos``/``size`` are not its
      planes' AABB (bit for bit); the detail is the largest difference.
    * ``material_mismatch``: a tagged convex face whose per-tag dict entry
      (texture, scale, shift, angle) differs from its plane's; the plane
      renders, the Surface Inspector shows the per-tag entry. The detail
      lists ``'tag.field'``.
    * ``ignored_plane_natural``: a plane flagged ``uv_natural`` whose face
      does not render natural (the renderer reads only the per-tag flag).
    * ``untextured_planes``: a convex brush the solid pass draws (no real
      texture in its per-tag dict) although planes name real textures; the
      detail lists those plane indices, drawn in the brush colour.
    * ``ignored_texture_tiling``: a convex brush with ``texture_tiling`` and
      a face with no scale and no natural flag, which a box would draw
      natural and the convex path draws stretched 0..1.
    """
    found = {}
    runtime = [key for key in RUNTIME_KEYS if brush.get(key) is not None]
    if runtime:
        found['runtime_keys'] = runtime
    if encoding(brush) != 'convex':
        return found
    convex = bg.get_convex(brush)
    lo, hi = convex.bounds
    centre, extent = (lo + hi) * 0.5, hi - lo
    pos, size = (np.asarray(v, dtype=np.float64) for v in bound(brush))
    if centre.tobytes() != pos.tobytes() or extent.tobytes() != size.tobytes():
        found['bound_drift'] = float(max(np.abs(centre - pos).max(),
                                         np.abs(extent - size).max()))
    mismatched = []
    for plane in brush['geometry']['planes']:
        tag = plane.get('face')
        if not tag:
            continue
        for key, field in TAG_FIELDS:
            entry = (brush.get(key) or {}).get(tag)
            if entry is not None and _tag_value(entry, field) != _plane_value(plane, field):
                mismatched.append('%s.%s' % (tag, field))
    if mismatched:
        found['material_mismatch'] = sorted(mismatched)
    ignored = [index for index, plane in enumerate(convex.planes)
               if plane.get('uv_natural')
               and not bg.face_uses_natural_scale(brush, plane.get('face'))]
    if ignored:
        found['ignored_plane_natural'] = ignored
    if draw_pass(brush) == 'solid':
        lost = [index for index, plane in enumerate(brush['geometry']['planes'])
                if plane.get('texture') and plane['texture'] not in UNTEXTURED
                and plane['texture'] != 'nodraw.jpg']
        if lost:
            found['untextured_planes'] = lost
    if brush.get('texture_tiling', False):
        stretched = sorted({index for index, (_tag, _ring, material)
                            in enumerate(render_faces(brush))
                            if material['uv_scale'] is None and not material['natural']})
        if stretched:
            found['ignored_texture_tiling'] = stretched
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
    """What :func:`snapshot` records for one map's brushes."""
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
        'digest': digest(brushes),
        'inconsistencies': dict(sorted(inventory.items())),
    }))


def shipped_maps(root):
    """``{file name: brushes}`` for every map in ``maps/`` that has brushes,
    each brush a shallow copy as ``EditorState._deserialize_brushes`` makes."""
    out = {}
    for path in sorted(glob.glob(os.path.join(root, 'maps', '*.json'))):
        with open(path) as handle:
            data = json.load(handle)
        brushes = [dict(brush) for brush in data.get('brushes') or []]
        if brushes:
            out[os.path.basename(path)] = brushes
    return out


def snapshot(root):
    """The semantics snapshot of every shipped map (the fixture's content)."""
    return {name: map_snapshot(brushes)
            for name, brushes in shipped_maps(root).items()}
