"""Overlay geometry the host builds for the renderer (no GL).

The renderer's editor and debug overlays take vertex arrays, not the
authored objects they are drawn for: a brush dictionary, the lists of
positions on brushes and Things, the collision runtime's brush-shaped
records. The host turns those into arrays here, in the thread that owns
them, and hands the arrays across the renderer contract.
"""

from __future__ import annotations

import numpy as np

from engine import brush_geometry

#: How far a face highlight sits off its surface (world units).
HIGHLIGHT_BIAS = 0.5

_NO_VERTICES = np.zeros((0, 3), dtype=np.float32)
_NO_COLOURS = np.zeros((0, 3), dtype=np.float32)


def face_highlight_triangles(brush, face_name):
    """World-space triangles ``(n, 3)`` float32 covering one face of *brush*
    (a box side by name, or a convex face of an angled brush), nudged off
    the surface; ``None`` when the face cannot be resolved."""
    if brush_geometry.brush_has_geometry(brush):
        return _convex_face_triangles(brush, face_name)
    pos, size = brush['pos'], brush['size']
    hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
    cx, cy, cz = pos[0], pos[1], pos[2]
    bias = HIGHLIGHT_BIAS
    if face_name == 'north':
        z = cz + hz + bias
        verts = [cx-hx, cy-hy, z, cx+hx, cy-hy, z, cx+hx, cy+hy, z,
                 cx-hx, cy-hy, z, cx+hx, cy+hy, z, cx-hx, cy+hy, z]
    elif face_name == 'south':
        z = cz - hz - bias
        verts = [cx+hx, cy-hy, z, cx-hx, cy-hy, z, cx-hx, cy+hy, z,
                 cx+hx, cy-hy, z, cx-hx, cy+hy, z, cx+hx, cy+hy, z]
    elif face_name == 'east':
        x = cx + hx + bias
        verts = [x, cy-hy, cz+hz, x, cy-hy, cz-hz, x, cy+hy, cz-hz,
                 x, cy-hy, cz+hz, x, cy+hy, cz-hz, x, cy+hy, cz+hz]
    elif face_name == 'west':
        x = cx - hx - bias
        verts = [x, cy-hy, cz-hz, x, cy-hy, cz+hz, x, cy+hy, cz+hz,
                 x, cy-hy, cz-hz, x, cy+hy, cz+hz, x, cy+hy, cz-hz]
    elif face_name == 'top':
        y = cy + hy + bias
        verts = [cx-hx, y, cz+hz, cx+hx, y, cz+hz, cx+hx, y, cz-hz,
                 cx-hx, y, cz+hz, cx+hx, y, cz-hz, cx-hx, y, cz-hz]
    elif face_name == 'down':
        y = cy - hy - bias
        verts = [cx-hx, y, cz-hz, cx+hx, y, cz-hz, cx+hx, y, cz+hz,
                 cx-hx, y, cz-hz, cx+hx, y, cz+hz, cx-hx, y, cz+hz]
    else:
        return None
    return np.asarray(verts, dtype=np.float32).reshape(-1, 3)


def _convex_face_triangles(brush, face_name):
    """A triangle fan over one convex face of an angled brush."""
    convex = brush_geometry.get_convex(brush)
    if convex is None or not convex.is_valid:
        return None
    face = None
    for f in convex.faces:
        if brush_geometry.face_key(f) == face_name:
            face = f
            break
    if face is None:
        return None
    idx = face['indices']
    if len(idx) < 3:
        return None
    ring = convex.verts[idx] + np.array(face['normal']) * HIGHLIGHT_BIAS
    out = []
    v0 = ring[0]
    for k in range(1, len(idx) - 1):
        for p in (v0, ring[k], ring[k + 1]):
            out.extend((float(p[0]), float(p[1]), float(p[2])))
    return np.asarray(out, dtype=np.float32).reshape(-1, 3)


def connection_segments(connections):
    """Editor I/O links as arrays: ``(segments (n, 2, 3), colours (n, 3))``
    float32, from ``{'src', 'dst', 'color'}`` records whose ends may be the
    live position lists of brushes and Things (read here, once a frame)."""
    n = len(connections)
    if not n:
        return _NO_VERTICES.reshape(0, 2, 3), _NO_COLOURS
    segments = np.empty((n, 2, 3), dtype=np.float32)
    colours = np.empty((n, 3), dtype=np.float32)
    for i, conn in enumerate(connections):
        segments[i, 0] = conn['src']
        segments[i, 1] = conn['dst']
        colours[i] = conn.get('color', (0.0, 1.0, 1.0))
    return segments, colours


#: Collision wireframe colours: boxes yellow, meshes cyan.
AABB_COLOUR = (1.0, 1.0, 0.0)
MESH_COLOUR = (0.0, 1.0, 1.0)

_BOX_EDGES = np.array([
    # bottom
    (-1, -1, -1, +1, -1, -1), (+1, -1, -1, +1, -1, +1),
    (+1, -1, +1, -1, -1, +1), (-1, -1, +1, -1, -1, -1),
    # top
    (-1, +1, -1, +1, +1, -1), (+1, +1, -1, +1, +1, +1),
    (+1, +1, +1, -1, +1, +1), (-1, +1, +1, -1, +1, -1),
    # verticals
    (-1, -1, -1, -1, +1, -1), (+1, -1, -1, +1, +1, -1),
    (+1, -1, +1, +1, +1, +1), (-1, -1, +1, -1, +1, +1),
], dtype=np.float32).reshape(24, 3)


def collision_wireframes(brushes):
    """Debug wireframes of collision records, in the records' order:
    ``(vertices (n, 3) float32, runs)`` where each run is
    ``(first vertex, vertex count, (r, g, b))`` of GL_LINES -- a yellow box
    for an AABB record, cyan triangle edges for a mesh record."""
    chunks, runs, first = [], [], 0
    for brush in brushes:
        if not brush.get('_model_collision'):
            continue
        mode = brush.get('_collision_mode', 'aabb')
        if mode == 'aabb':
            # In double precision, rounded once to float32, as it always was.
            pos = np.asarray(brush.get('pos', [0, 0, 0]), dtype=np.float64)
            half = np.asarray(brush.get('size', [64, 64, 64]), dtype=np.float64) / 2
            lines, colour = pos + _BOX_EDGES.astype(np.float64) * half, AABB_COLOUR
        elif mode == 'mesh':
            triangles = brush.get('_mesh_triangles', [])
            if not triangles:
                continue
            corners = np.asarray([tri for tri, _normal in triangles],
                                 dtype=np.float32).reshape(-1, 3, 3)
            # Each triangle's three edges: 0-1, 1-2, 2-0.
            lines = corners[:, (0, 1, 1, 2, 2, 0), :].reshape(-1, 3)
            colour = MESH_COLOUR
        else:
            continue
        lines = np.ascontiguousarray(lines, dtype=np.float32)
        chunks.append(lines)
        runs.append((first, len(lines), colour))
        first += len(lines)
    if not chunks:
        return _NO_VERTICES, ()
    return np.concatenate(chunks), tuple(runs)
