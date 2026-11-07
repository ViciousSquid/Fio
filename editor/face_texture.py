"""Reading and writing one brush face's texture transform.

Every face has one material record (:mod:`engine.brush_faces`):

* a **box face** — one of the six tagged sides of a box brush — keeps it in
  the brush's per-tag dicts (``brush['uv_shift']['east']`` and friends);
* a face of an **angled brush** — tagged side or cut face alike — keeps it on
  the plane that generates the face.

The Surface Inspector shows that record and edits it, and the renderer draws
it, so the two cannot disagree. A legacy angled brush that still carries a
per-tag copy is read by :data:`engine.brush_faces.PRECEDENCE`, and an edit
leaves the face with its one record. The module is deliberately Qt-free so
the Surface Inspector's behaviour can be tested without a window, and so the
same accessors can be reused by anything else that needs to touch a face's
mapping.
"""

import numpy as np

from engine import brush_faces
from engine import brush_geometry as bg


# A face with no explicit mapping behaves as though it had these.
DEFAULT_SHIFT = (0.0, 0.0)
DEFAULT_SCALE = (1.0, 1.0)
DEFAULT_ANGLE = 0.0

# Fallback texture size when the real one cannot be read; matches the
# renderer's own fallback so "Natural" agrees with what is drawn.
DEFAULT_TEXTURE_SIZE = (128, 128)

# Which two of the brush's dimensions a tagged box face spans, matching the
# renderer's cube UV layout.
_TAG_EXTENT_AXES = {
    'south': (0, 1), 'north': (0, 1),
    'west': (2, 1), 'east': (2, 1),
    'down': (0, 2), 'top': (0, 2),
}


def face_plane(brush, face_key):
    """The plane backing ``face_key``, or ``None`` for a plain box face."""
    if not bg.brush_has_geometry(brush):
        return None
    index = bg.face_plane_index(brush, face_key)
    if index is None:
        return None
    planes = brush.get('geometry', {}).get('planes', [])
    if not (0 <= index < len(planes)):
        return None
    return planes[index]


def is_cut_face(brush, face_key):
    """True when the face has no box tag, so its mapping lives on its plane."""
    plane = face_plane(brush, face_key)
    return plane is not None and not plane.get('face')


def is_natural(brush, face_key):
    """True when the face is in Natural mode (constant texel size)."""
    return bool(brush_faces.face_record(brush, face_key).get('uv_natural'))


def get_transform(brush, face_key, texture_size=None):
    """The face's mapping as ``{'shift', 'scale', 'angle', 'texture', 'natural'}``.

    Read from the face's one material record. Values are plain
    floats/tuples, defaulted when the face has never been touched;
    ``texture`` is ``None`` when the face names none.

    A face in Natural mode has no fixed scale — the renderer derives it from
    the face's current size — so pass ``texture_size`` to get the repeats it
    is *currently* drawing with rather than whatever was last written down.
    """
    record = brush_faces.face_record(brush, face_key)
    natural = bool(record.get('uv_natural'))
    scale = tuple(float(v) for v in (record.get('uv_scale') or DEFAULT_SCALE))
    if natural and texture_size is not None:
        scale = natural_scale(brush, face_key, texture_size)
    angle = record.get('uv_angle')
    return {
        'texture': record.get('texture'),
        'shift': tuple(float(v) for v in (record.get('uv_shift') or DEFAULT_SHIFT)),
        'scale': scale,
        'angle': float(DEFAULT_ANGLE if angle is None else angle),
        'natural': natural,
    }


def set_transform(brush, face_key, shift=None, scale=None, angle=None,
                  texture=None, natural=None):
    """Write any subset of a face's mapping to its one material record.

    ``None`` leaves a value as it is. Setting an explicit ``scale`` turns
    Natural mode off: the user has just said what the repeats should be, so
    the face must stop deriving them from its own size.  Pass ``natural``
    explicitly to set the mode itself. An angled brush's derived geometry —
    and the GPU mesh the renderer built from it, which bakes every face's
    mapping — is invalidated by the write.
    """
    if scale is not None and natural is None:
        natural = False
    fields = {}
    if shift is not None:
        fields['uv_shift'] = shift
    if scale is not None:
        fields['uv_scale'] = scale
    if angle is not None:
        fields['uv_angle'] = angle
    if texture is not None:
        fields['texture'] = texture
    if natural is not None:
        fields['uv_natural'] = bool(natural)
    if fields:
        brush_faces.set_face_material(brush, face_key, **fields)


def face_keys(brush):
    """Every addressable face of ``brush``, tagged sides and cut faces alike."""
    if bg.brush_has_geometry(brush):
        return [key for key, _ in bg.iter_surface_faces(brush)]
    return list(bg.FACE_TAGS)


def face_extent(brush, face_key):
    """The face's size in world units along its texture axes, as ``(w, h)``.

    This is what "one repeat of the texture" has to span, so it is the basis
    for both Fit and Natural.
    """
    if bg.brush_has_geometry(brush):
        convex = bg.get_convex(brush)
        if convex is not None and convex.is_valid:
            for face in convex.faces:
                if bg.face_key(face) != face_key:
                    continue
                ring = convex.verts[face['indices']]
                _, _, (_, eu), (_, ev) = bg.face_uv_projection(ring, face)
                return float(eu), float(ev)
    size = brush.get('size', [64, 64, 64])
    axes = _TAG_EXTENT_AXES.get(face_key)
    if axes is None:
        return float(size[0]), float(size[1])
    return float(size[axes[0]]), float(size[axes[1]])


def fit_scale(repeat=(1.0, 1.0)):
    """Repeat factors that make the texture span the face ``repeat`` times.

    Face UVs are normalised to 0..1 across the face, so fitting is simply the
    repeat count — no measuring needed, at any face size or shape.
    """
    return float(repeat[0]), float(repeat[1])


def natural_scale(brush, face_key, texture_size=DEFAULT_TEXTURE_SIZE):
    """Repeat factors for the texture at its natural size: 1 texel per unit.

    A 128x128 texture on a 512-unit wall repeats four times across it, which is
    what a mapper means by "natural" — the texture is neither stretched nor
    squashed, whatever the face's dimensions.
    """
    width, height = face_extent(brush, face_key)
    tex_w = max(float(texture_size[0]), 1.0)
    tex_h = max(float(texture_size[1]), 1.0)
    return width / tex_w, height / tex_h


def apply_fit(brush, face_key, repeat=(1.0, 1.0)):
    """Fit the texture across the face, clearing any rotation or offset."""
    set_transform(brush, face_key, shift=DEFAULT_SHIFT,
                  scale=fit_scale(repeat), angle=0.0)


def apply_natural(brush, face_key, texture_size=DEFAULT_TEXTURE_SIZE):
    """Switch the face to Natural mode: a constant texel size, forever.

    The scale is written too, but only as a fallback for anything that reads
    the mapping without knowing about the mode; the renderer recomputes it from
    the face's current size, so resizing the brush reveals more of the texture
    instead of stretching it.
    """
    set_transform(brush, face_key, shift=DEFAULT_SHIFT,
                  scale=natural_scale(brush, face_key, texture_size),
                  natural=True)


def apply_axial(brush, face_key, texture_size=DEFAULT_TEXTURE_SIZE):
    """Reset the face to a plain world-axis projection at natural size.

    This is the escape hatch from texture lock: a face that has been rotated
    with its brush carries a texture basis that turned with it, and clearing
    that basis drops the face back onto the world axes — the mapping it would
    have had if it had never been rotated.
    """
    plane = face_plane(brush, face_key)
    if plane is not None:
        plane.pop('uv_u', None)
        plane.pop('uv_v', None)
        bg.invalidate_geometry_cache(brush)
    set_transform(brush, face_key, shift=DEFAULT_SHIFT, angle=0.0,
                  scale=natural_scale(brush, face_key, texture_size))


def flip(brush, face_key, horizontal=False, vertical=False):
    """Mirror the texture on the face by negating a scale axis."""
    current = get_transform(brush, face_key)
    su, sv = current['scale']
    if horizontal:
        su = -su
    if vertical:
        sv = -sv
    set_transform(brush, face_key, scale=(su, sv))


def match_grid(brush, face_key, steps):
    """Snap each value to the nearest multiple of its own step.

    ``steps`` is ``{'shift': (h, v), 'scale': (h, v), 'angle': a}``; a step of
    zero leaves that value alone.
    """
    current = get_transform(brush, face_key)

    def _snap(value, step):
        return round(value / step) * step if step else value

    shift_step = steps.get('shift', (0.0, 0.0))
    scale_step = steps.get('scale', (0.0, 0.0))
    angle_step = steps.get('angle', 0.0)
    set_transform(
        brush, face_key,
        shift=(_snap(current['shift'][0], shift_step[0]),
               _snap(current['shift'][1], shift_step[1])),
        scale=(_snap(current['scale'][0], scale_step[0]),
               _snap(current['scale'][1], scale_step[1])),
        angle=_snap(current['angle'], angle_step),
    )
