"""Reading and writing one brush face's texture transform.

A face's mapping lives in two different places depending on what kind of face
it is, which is the main reason texturing angled geometry has been awkward:

* a **box face** — one of the six tagged sides — keeps its transform in the
  brush's per-tag dicts (``brush['uv_shift']['east']`` and friends), because
  the renderer draws those from the shared cube VAO and passes the transform
  as uniforms;
* a **cut face** — produced by the clip tool, or any face of a brush that has
  become an angled plane set — has no tag to key off, so its transform lives
  on the plane itself.

Everything here presents those as one thing: give it a brush and a face key
and it reads or writes the right place.  The module is deliberately Qt-free so
the Surface Inspector's behaviour can be tested without a window, and so the
same accessors can be reused by anything else that needs to touch a face's
mapping.
"""

import numpy as np

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


def _brush_dict(brush, key):
    value = brush.get(key)
    return value if isinstance(value, dict) else {}


def is_natural(brush, face_key):
    """True when the face is in Natural mode (constant texel size)."""
    plane = face_plane(brush, face_key)
    cut = plane is not None and not plane.get('face')
    return bg.face_uses_natural_scale(brush, None if cut else face_key, plane)


def get_transform(brush, face_key, texture_size=None):
    """The face's mapping as ``{'shift', 'scale', 'angle', 'texture', 'natural'}``.

    Values are plain floats/tuples, defaulted when the face has never been
    touched, so callers never have to know which storage a face uses.

    A face in Natural mode has no fixed scale — the renderer derives it from
    the face's current size — so pass ``texture_size`` to get the repeats it
    is *currently* drawing with rather than whatever was last written down.
    """
    plane = face_plane(brush, face_key)
    cut = plane is not None and not plane.get('face')

    if cut:
        texture = plane.get('texture')
        scale = plane.get('uv_scale')
        shift = plane.get('uv_shift')
        angle = plane.get('uv_angle')
    else:
        texture = _brush_dict(brush, 'textures').get(face_key)
        scale = _brush_dict(brush, 'uv_scale').get(face_key)
        shift = _brush_dict(brush, 'uv_shift').get(face_key)
        angle = _brush_dict(brush, 'uv_angle').get(face_key)
        if scale is None and plane is not None:
            # A tagged face of an angled brush may still carry its scale on the
            # plane (that is where the clip tool puts inherited values).
            scale = plane.get('uv_scale')

    natural = bg.face_uses_natural_scale(brush, None if cut else face_key, plane)
    scale = tuple(float(v) for v in (scale or DEFAULT_SCALE))
    if natural and texture_size is not None:
        scale = natural_scale(brush, face_key, texture_size)

    return {
        'texture': texture,
        'shift': tuple(float(v) for v in (shift or DEFAULT_SHIFT)),
        'scale': scale,
        'angle': float(DEFAULT_ANGLE if angle is None else angle),
        'natural': natural,
    }


def set_transform(brush, face_key, shift=None, scale=None, angle=None,
                  texture=None, natural=None):
    """Write any subset of a face's mapping to wherever that face keeps it.

    Setting an explicit ``scale`` turns Natural mode off: the user has just
    said what the repeats should be, so the face must stop deriving them from
    its own size.  Pass ``natural`` explicitly to set the mode itself.
    """
    plane = face_plane(brush, face_key)
    cut = plane is not None and not plane.get('face')
    if scale is not None and natural is None:
        natural = False

    if cut:
        if shift is not None:
            plane['uv_shift'] = [float(shift[0]), float(shift[1])]
        if scale is not None:
            plane['uv_scale'] = [float(scale[0]), float(scale[1])]
        if angle is not None:
            plane['uv_angle'] = float(angle)
        if texture is not None:
            plane['texture'] = texture
        if natural is not None:
            if natural:
                plane['uv_natural'] = True
            else:
                plane.pop('uv_natural', None)
        # A face winding carries its own texture, UV scale and texture basis, so
        # editing the plane's mapping makes the cached geometry — and the GPU
        # mesh the renderer built from it — as stale as moving the plane would.
        bg.invalidate_geometry_cache(brush)
        return

    if shift is not None:
        brush.setdefault('uv_shift', {})[face_key] = [float(shift[0]), float(shift[1])]
    if scale is not None:
        brush.setdefault('uv_scale', {})[face_key] = [float(scale[0]), float(scale[1])]
    if angle is not None:
        brush.setdefault('uv_angle', {})[face_key] = float(angle)
    if texture is not None:
        brush.setdefault('textures', {})[face_key] = texture
        # A tagged face of an angled brush is drawn from its plane, so the
        # plane has to learn about the new texture as well — and the derived
        # winding that copied the old one has to be dropped.
        if plane is not None:
            plane['texture'] = texture
            bg.invalidate_geometry_cache(brush)
    if natural is not None:
        if natural:
            brush.setdefault('uv_natural', {})[face_key] = True
        else:
            brush.get('uv_natural', {}).pop(face_key, None)


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
