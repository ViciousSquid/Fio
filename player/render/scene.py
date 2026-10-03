"""Turn a parsed Fio map into GPU-ready geometry (Milestone 3).

This is the first real slice of the engine-render bridge: it converts a map's
``brushes`` into a single interleaved vertex buffer of lit boxes, and pulls the
``light`` entities out of ``things`` so the ``lit`` shader can shade them.

It is deliberately small and dependency-light (numpy only) so it is unit-testable
off-device. It does not yet handle textures, non-box brush shapes, portals,
water/glass shaders, or models — those are later milestones. The output feeds
:class:`player.render.renderer.GLESRenderer`, which owns the GL upload/draw.

Vertex layout (matches the ``lit`` shader's attributes):
    location 0: position (vec3, world space)
    location 1: normal   (vec3, world space)
Stride is 6 floats; brush vertices are baked into world space so the draw uses
an identity model matrix.
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
_FACE_KEYS = ("south", "north", "east", "west", "top", "down")
_FACE_UV_AXES = ((0, 1), (0, 1), (2, 1), (2, 1), (0, 2), (0, 2))

FLOATS_PER_VERTEX_TEX = 8       # pos3 + normal3 + uv2
DEFAULT_TEXEL = 128.0           # world units per texture repeat


def _is_renderable(brush: dict) -> bool:
    if not isinstance(brush, dict):
        return False
    if brush.get("hidden") or brush.get("is_fog"):
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
        for normal, corners in _FACES:
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

    return out.reshape(-1), row


def build_textured_batches(
    map_data: dict, texel: float = DEFAULT_TEXEL
) -> Dict[str, "np.ndarray"]:
    """Group brush faces by texture into interleaved (pos3, normal3, uv2) arrays.

    Returns ``{texture_name: vertices}``. Faces with no texture assigned land
    under the empty-string key ``""`` (drawn untextured by the renderer). UVs
    tile every ``texel`` world units so textures keep a constant real-world size
    regardless of face dimensions (Radiant-style), and repeat via GL_REPEAT.
    """
    batches: Dict[str, list] = {}
    for b in map_data.get("brushes", []):
        if not _is_renderable(b):
            continue
        cx, cy, cz = (float(v) for v in b["pos"])
        size = [abs(float(v)) for v in b["size"]]
        sx, sy, sz = (float(v) for v in b["size"])
        textures = b.get("textures") if isinstance(b.get("textures"), dict) else {}

        for fi, (normal, corners) in enumerate(_FACES):
            texname = textures.get(_FACE_KEYS[fi]) or ""
            ua, va = _FACE_UV_AXES[fi]
            span_u = size[ua] / texel if texel else 1.0
            span_v = size[va] / texel if texel else 1.0
            group = batches.setdefault(texname, [])
            for a, bi, c in ((0, 1, 2), (0, 2, 3)):
                for idx in (a, bi, c):
                    ox, oy, oz = corners[idx]
                    group.extend((
                        cx + ox * sx, cy + oy * sy, cz + oz * sz,
                        normal[0], normal[1], normal[2],
                        (corners[idx][ua] + 0.5) * span_u,
                        (corners[idx][va] + 0.5) * span_v,
                    ))
    return {name: np.asarray(v, dtype=np.float32) for name, v in batches.items() if v}


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
