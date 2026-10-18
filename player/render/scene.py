"""Build ES vertex batches for box and authored convex brushes.

Textures come from the world package; clipped surfaces use the engine's shared
convex geometry. Vertex layout is position3, normal3, uv2. The older untextured
builder is retained for callers and tests.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

import numpy as np

# Unit cube: 6 faces, each (outward normal, 4 corner offsets in CCW order).
# Corner offsets are in [-0.5, 0.5]; scaled by brush size and shifted by center.
_FACES = [
    ((0, 0, 1), [(-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)]),   # +Z
    ((0, 0, -1), [(0.5, -0.5, -0.5), (-0.5, -0.5, -0.5), (-0.5, 0.5, -0.5), (0.5, 0.5, -0.5)]),  # -Z
    ((1, 0, 0), [(0.5, -0.5, 0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (0.5, 0.5, 0.5)]),   # +X
    ((-1, 0, 0), [(-0.5, -0.5, -0.5), (-0.5, -0.5, 0.5), (-0.5, 0.5, 0.5), (-0.5, 0.5, -0.5)]),  # -X
    ((0, 1, 0), [(-0.5, 0.5, 0.5), (0.5, 0.5, 0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5)]),   # +Y
    ((0, -1, 0), [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, -0.5, 0.5), (-0.5, -0.5, 0.5)]),  # -Y
]

FLOATS_PER_VERTEX = 6
VERTS_PER_BRUSH = 36  # 6 faces * 2 triangles * 3 verts

# Per-face metadata, index-aligned with _FACES:
#   * the map's texture key for that face
#   * (u_axis, v_axis) — which size components span the face, for UV tiling
_FACE_KEYS = ("north", "south", "east", "west", "top", "down")
_FACE_UV_AXES = ((0, 1), (0, 1), (2, 1), (2, 1), (0, 2), (0, 2))

FLOATS_PER_VERTEX_TEX = 8       # pos3 + normal3 + uv2
DEFAULT_TEXEL = 128.0           # world units per texture repeat


def _is_visible_surface(texture) -> bool:
    # Reserved surface names may be bare, packaged paths or image filenames.
    basename = str(texture or '').replace('\\', '/').rsplit('/', 1)[-1].lower()
    return basename.rsplit('.', 1)[0] not in ('nodraw', 'caulk')


def _is_renderable(brush: dict) -> bool:
    if not isinstance(brush, dict):
        return False
    if brush.get("hidden") or brush.get("is_fog") or brush.get('shader') == 'Fog':
        return False
    # Non-solid, non-visual trigger volumes are skipped.
    if brush.get("is_trigger") and not (brush.get("is_door") or brush.get("is_mover")):
        return False
    return "pos" in brush and "size" in brush


def build_brush_mesh(map_data: dict) -> Tuple["np.ndarray", int]:
    """Build one interleaved (pos3, normal3) vertex array for all brushes.

    Returns ``(vertices, vertex_count)``; ``vertices`` is a flat float32 array.
    """
    brushes = [b for b in map_data.get("brushes", []) if _is_renderable(b)]
    out = np.empty((len(brushes) * VERTS_PER_BRUSH, FLOATS_PER_VERTEX), dtype=np.float32)

    row = 0
    for b in brushes:
        cx, cy, cz = (float(v) for v in b["pos"])
        sx, sy, sz = (float(v) for v in b["size"])
        for tag, (normal, corners) in zip(_FACE_KEYS, _FACES):
            if not _is_visible_surface((b.get('textures') or {}).get(tag)):
                continue
            # Two triangles per quad: (0,1,2) and (0,2,3).
            for a, bi, c in ((0, 1, 2), (0, 2, 3)):
                for idx in (a, bi, c):
                    ox, oy, oz = corners[idx]
                    out[row, 0] = cx + ox * sx
                    out[row, 1] = cy + oy * sy
                    out[row, 2] = cz + oz * sz
                    out[row, 3] = normal[0]
                    out[row, 4] = normal[1]
                    out[row, 5] = normal[2]
                    row += 1

    return out[:row].reshape(-1), row


def build_textured_batches(map_data: dict, texel=None, texture_size=None):
    """Use the desktop Surface Inspector mapping: Fit/Natural, rotate, scale, shift."""
    from engine.brush_geometry import get_convex, face_uv_projection, face_uses_natural_scale
    batches = {}
    texture_size = texture_size or (lambda name: (128, 128))

    def emit(brush, name, points, normal, uv, extent, meta, tag=None):
        # Omit geometry entirely, including from the shared shadow batches.
        if not _is_visible_surface(name):
            return
        scale = meta.get('uv_scale')
        natural = face_uses_natural_scale(brush, tag, meta)
        if natural or (scale is None and brush.get('texture_tiling')):
            tw, th = texture_size(name)
            scale = (extent[0] / max(1, tw), extent[1] / max(1, th))
        elif scale is None:
            scale = (extent[0] / texel, extent[1] / texel) if texel else (1, 1)
        angle = math.radians(float(meta.get('uv_angle', 0)))
        c, sn = math.cos(angle), math.sin(angle)
        shift = meta.get('uv_shift') or (0, 0)
        output = batches.setdefault(name or '', [])
        for j in range(1, len(points) - 1):
            for index in (0, j, j + 1):
                u, v = uv[index][0] - .5, uv[index][1] - .5
                u, v = (u*c-v*sn+.5)*scale[0]+shift[0], (u*sn+v*c+.5)*scale[1]+shift[1]
                output.extend((*points[index], *normal, u, v))

    for brush in map_data.get('brushes', []):
        if not _is_renderable(brush):
            continue
        if brush.get('geometry', {}).get('planes'):
            convex = get_convex(brush)
            for face in convex.faces:
                points = convex.verts[face['indices']]
                us, vs, (u0, eu), (v0, ev) = face_uv_projection(points, face)
                plane = convex.planes[face['plane']]
                uv = list(zip((us-u0)/eu, (vs-v0)/ev))
                emit(brush, face.get('texture') or '', points, face['normal'], uv,
                     (eu, ev), plane, face.get('face'))
            continue
        size = np.asarray(brush['size'], dtype=float)
        center = np.asarray(brush['pos'], dtype=float)
        for index, (normal, corners) in enumerate(_FACES):
            tag = _FACE_KEYS[index]
            name = (brush.get('textures') or {}).get(tag) or ''
            corners = np.asarray(corners)
            # These are the UV bases of the existing desktop cube VAO.
            if index < 2:
                uv = corners[:, (0, 1)] + .5
            elif index < 4:
                uv = np.column_stack((corners[:, 1]+.5, .5-corners[:, 2]))
            else:
                uv = np.column_stack((corners[:, 0]+.5, .5-corners[:, 2]))
            ua, va = _FACE_UV_AXES[index]
            meta = {key: (brush.get(key) or {}).get(tag) for key in ('uv_scale', 'uv_angle', 'uv_shift', 'uv_natural')}
            meta['uv_angle'] = meta['uv_angle'] or 0
            emit(brush, name, center+corners*size, normal, uv,
                 (abs(size[ua]), abs(size[va])), meta, tag)
    return {name: np.asarray(values, dtype=np.float32) for name, values in batches.items() if values}


def extract_lights(map_data: dict, limit: int = 8) -> List[Dict]:
    """Pull point lights out of the map's ``things`` (max ``limit``).

    Each returned light: ``{pos:(x,y,z), color:(r,g,b) 0..1, intensity, radius}``.
    Matches the ``lit`` shader's ``Light`` struct (shadowIndex is set to -1 by
    the renderer since shadow cube-maps are a later milestone).
    """
    lights: List[Dict] = []
    for thing in map_data.get("things", []):
        if not isinstance(thing, dict):
            continue
        if str(thing.get("type", "")).lower() != "light":
            continue
        props = thing.get("properties", {}) if isinstance(thing.get("properties"), dict) else {}
        pos = thing.get("pos") or props.get("pos") or [0, 0, 0]
        if props.get("state", "on") == "off":
            continue
        colour = props.get("colour") or props.get("color") or [255, 255, 255]
        try:
            color = tuple(max(0.0, min(1.0, float(c) / 255.0)) for c in colour[:3])
        except (TypeError, ValueError):
            color = (1.0, 1.0, 1.0)
        lights.append({
            "pos": tuple(float(v) for v in pos[:3]),
            "color": color,
            "intensity": float(props.get("intensity", 1.0)),
            "radius": float(props.get("radius", 512.0)),
        })
        if len(lights) >= limit:
            break
    return lights


def scene_bounds(map_data: dict):
    """Axis-aligned bounds of all renderable brushes, or None if empty.

    Handy for placing/So framing the camera when a map has no PlayerStart.
    """
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    found = False
    for b in map_data.get("brushes", []):
        if not _is_renderable(b):
            continue
        found = True
        for i in range(3):
            c = float(b["pos"][i])
            h = abs(float(b["size"][i])) * 0.5
            lo[i] = min(lo[i], c - h)
            hi[i] = max(hi[i], c + h)
    if not found:
        return None
    return tuple(lo), tuple(hi)
