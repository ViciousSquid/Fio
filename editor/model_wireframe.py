"""Model wireframes for the 2D views, in bulk.

The 2D views draw every model as its wireframe. They used to walk the
triangles in Python and issue three ``QPainter.drawLine`` calls for each:
about 18,000 calls for a 6,000-triangle chair, per view, per repaint -- and a
drag repaints all three views on every mouse move. Twelve chairs took a 2D
repaint to ~300 ms.

Here the work is split by how often its inputs change:

* :func:`model_edges` -- once per loaded model: the model's distinct corner
  positions (a flat OBJ repeats each shared corner per triangle; they are
  welded) and its distinct edges (an edge shared by two triangles is one
  line, not two).
* :func:`project` -- per model per paint: one NumPy transform of the welded
  positions to screen space (also what the views hit-test against).
* :func:`visible_segments` -- per model per paint: the edges that can show,
  snapped to whole pixels and deduplicated, so a model far away (dozens of
  edges on one pixel) costs a handful of lines however many triangles it has.

The result is drawn with one ``drawLines`` call over a ``QPolygonF`` filled
straight from the NumPy buffer (:func:`segments_polygon`). Everything but that
last step is Qt-free.
"""

from __future__ import annotations

import math

import numpy as np

#: Edges above which a model is drawn as a box instead of a wireframe.
MAX_WIREFRAME_EDGES = 200_000

# id(model) -> (model, positions, edges), least recently used first. The
# model is held so its id cannot be reused by another object while the entry
# exists; past the limit the least recently used entry goes (clearing the lot
# made a level with more models than the limit rebuild nearly every model's
# edges on every repaint).
_EDGE_CACHE: dict = {}
_EDGE_CACHE_LIMIT = 256


def _triangles(model, vertex_count):
    tris = getattr(model, 'cpu_triangles', None)
    if tris is not None and len(tris):
        tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    else:
        # Non-indexed: corners laid out as sequential flat triangles.
        n = vertex_count - vertex_count % 3
        tris = np.arange(n, dtype=np.int64).reshape(-1, 3)
    # Malformed index data is skipped, as the per-triangle loop did.
    ok = ((tris >= 0) & (tris < vertex_count)).all(axis=1)
    return tris if ok.all() else tris[ok]


def build_edges(vertices, model=None):
    """``(positions, edges)`` for a model's corner *vertices*.

    *positions* is ``(N, 3)`` float64, the distinct corners; *edges* is
    ``(M, 2)`` int32 indices into it, each edge once.
    """
    verts = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    if not len(verts):
        return verts, np.empty((0, 2), dtype=np.int32)
    tris = _triangles(model, len(verts))
    positions, inverse = np.unique(verts, axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    tris = inverse[tris]
    edges = tris[:, (0, 1, 1, 2, 2, 0)].reshape(-1, 2)
    edges.sort(axis=1)
    edges = edges[edges[:, 0] != edges[:, 1]]
    if len(edges):
        # One int64 key per edge: unique on it is much cheaper than on rows.
        keys = np.unique(edges[:, 0] * len(positions) + edges[:, 1])
        edges = np.stack((keys // len(positions), keys % len(positions)), axis=1)
    return positions, edges.astype(np.int32, copy=False)


def model_edges(model):
    """:func:`build_edges` for a loaded model, computed once and cached.

    None when the model has no CPU-side corners.
    """
    key = id(model)
    hit = _EDGE_CACHE.pop(key, None)
    if hit is not None and hit[0] is model:
        _EDGE_CACHE[key] = hit                  # most recently used last
        return None if hit[1] is None else (hit[1], hit[2])
    vertices = getattr(model, 'cpu_vertices', None)
    if vertices is None or len(vertices) == 0:
        return None
    positions, edges = build_edges(vertices, model)
    if not np.isfinite(positions).all():
        # A NaN or infinite corner (an OBJ may spell one) has no place on
        # screen: the model is drawn as a box. Remembered, not rebuilt.
        positions = edges = None
    while len(_EDGE_CACHE) >= _EDGE_CACHE_LIMIT:
        _EDGE_CACHE.pop(next(iter(_EDGE_CACHE)))
    _EDGE_CACHE[key] = (model, positions, edges)
    return None if positions is None else (positions, edges)


def placement(scale, rotation):
    """``(scale, rotation)`` as three finite floats each, or None.

    Both come from map data and the property panel: a malformed or
    non-finite value is drawn as a box instead of raising in a paint event.
    """
    try:
        if isinstance(scale, (int, float)):
            scale = (float(scale),) * 3
        else:
            scale = tuple(float(v) for v in scale[:3])
        rotation = tuple(float(v) for v in rotation[:3])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if len(scale) != 3 or len(rotation) != 3:
        return None
    if not all(math.isfinite(v) for v in scale + rotation):
        return None
    return scale, rotation


def rotation_matrix(rotation_degrees):
    """The model rotation the views have always used: Z * X * Y."""
    rx, ry, rz = (math.radians(float(a)) for a in rotation_degrees[:3])
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    mx = np.array(((1, 0, 0), (0, cx, -sx), (0, sx, cx)))
    my = np.array(((cy, 0, sy), (0, 1, 0), (-sy, 0, cy)))
    mz = np.array(((cz, -sz, 0), (sz, cz, 0), (0, 0, 1)))
    return mz @ mx @ my


def project(positions, pos, scale, rotation_degrees, axes, centre, pan, zoom,
            flip_y):
    """Screen ``(xs, ys)`` of model-space *positions* placed at *pos*.

    *axes* are the world axis indices shown horizontally and vertically;
    *flip_y* is True for the side views, whose world up is screen up.
    """
    if isinstance(scale, (int, float)):
        scale = (scale, scale, scale)
    m = rotation_matrix(rotation_degrees).T * np.asarray(scale, dtype=np.float64)[:, None]
    i1, i2 = axes
    # Only the two shown world axes are ever computed.
    cols = m[:, (i1, i2)]
    world = positions @ cols
    xs = centre[0] + (world[:, 0] + (float(pos[i1]) - pan[0])) * zoom
    if flip_y:
        ys = centre[1] - (world[:, 1] + (float(pos[i2]) - pan[1])) * zoom
    else:
        ys = centre[1] + (world[:, 1] + (float(pos[i2]) - pan[1])) * zoom
    return xs, ys


def visible_segments(xs, ys, edges, width, height):
    """``(K, 4)`` float64 segments ``x0, y0, x1, y1`` worth drawing.

    Edges wholly to one side of the view are dropped (the per-triangle test
    the loop made), the rest are snapped to whole pixels -- what a 1-pixel,
    non-antialiased pen draws anyway -- and a segment that lands on the same
    pixels as another is drawn once.
    """
    if not len(edges):
        return np.empty((0, 4))
    a, b = edges[:, 0], edges[:, 1]
    x0, x1, y0, y1 = xs[a], xs[b], ys[a], ys[b]
    keep = ~(((x0 < 0) & (x1 < 0)) | ((x0 > width) & (x1 > width))
             | ((y0 < 0) & (y1 < 0)) | ((y0 > height) & (y1 > height)))
    if not keep.any():
        return np.empty((0, 4))
    seg = np.rint(np.stack((x0[keep], y0[keep], x1[keep], y1[keep]), axis=1))
    # Same segment either way round: order the endpoints, then draw each
    # distinct pixel segment once. The key packs four 16-bit coordinates, so
    # it is used only when every endpoint fits (a zoomed-in edge reaching far
    # off screen skips the dedupe rather than risk merging distinct lines).
    swap = (seg[:, 0] > seg[:, 2]) | ((seg[:, 0] == seg[:, 2]) & (seg[:, 1] > seg[:, 3]))
    seg[swap] = seg[swap][:, (2, 3, 0, 1)]
    limit = 1 << 15
    if len(seg) > 1 and (seg >= -limit).all() and (seg < limit).all():
        q = (seg + limit).astype(np.uint64)
        key = ((q[:, 0] << np.uint64(48)) | (q[:, 1] << np.uint64(32))
               | (q[:, 2] << np.uint64(16)) | q[:, 3])
        _, first = np.unique(key, return_index=True)
        if len(first) < len(seg):
            seg = seg[np.sort(first)]
    return seg


def segments_polygon(segments):
    """A ``QPolygonF`` of the segments' endpoint pairs, for ``drawLines``,
    filled from the NumPy buffer rather than point by point."""
    from PyQt5.QtGui import QPolygonF
    count = 2 * len(segments)
    polygon = QPolygonF(count)
    if count:
        buffer = polygon.data()
        buffer.setsize(count * 2 * 8)
        np.frombuffer(buffer, dtype=np.float64).reshape(-1, 4)[:] = segments
    return polygon


# ---------------------------------------------------------------------------
# Cached wireframe images
# ---------------------------------------------------------------------------
#
# Dragging, nudging or panning moves models without changing what their
# wireframes look like, and a level usually repeats a few models many times.
# A wireframe is therefore rasterised once per (model, rotation, scale, zoom,
# view axes) into a transparent image, and every instance is that image drawn
# at its position: one rasterisation for twelve identical chairs, none at all
# while they are dragged. Source-over is associative, so the result matches
# drawing the lines straight onto the view (to within the sub-pixel snap of
# the model's origin).

#: Largest side, in pixels, of a cached wireframe image. A model bigger than
#: this on screen (zoomed right in) is drawn directly, culled to the view.
MAX_IMAGE_SIDE = 1024
#: Pixels of cached images kept before the oldest are dropped (~64 MB).
IMAGE_CACHE_PIXELS = 16 * 1024 * 1024
#: What each entry is charged on top of its pixels: its Python and Qt objects
#: cost about this much again, and without it thousands of tiny images (props
#: scattered at random yaws, zoom steps) fitted the pixel budget for gigabytes.
IMAGE_ENTRY_COST = 1024
#: Entries kept at most, whatever their size.
IMAGE_CACHE_ENTRIES = 4096

_IMAGE_CACHE: dict = {}
_image_cache_pixels = 0


def _image_key(model, scale, rotation, axes, zoom, flip_y, colour):
    if isinstance(scale, (int, float)):
        scale = (scale, scale, scale)
    return (id(model), tuple(float(v) for v in scale[:3]),
            tuple(float(v) for v in rotation[:3]), tuple(axes), float(zoom),
            bool(flip_y), tuple(colour))


def wireframe_image(model, positions, edges, scale, rotation, axes, zoom,
                    flip_y, colour):
    """``(image, left, top)``: the wireframe of *model* as a transparent
    ``QImage``, and where the model's origin falls inside it (``-left``,
    ``-top``); None when it would exceed :data:`MAX_IMAGE_SIDE`."""
    global _image_cache_pixels
    key = _image_key(model, scale, rotation, axes, zoom, flip_y, colour)
    hit = _IMAGE_CACHE.pop(key, None)
    if hit is not None and hit[0] is model:
        _IMAGE_CACHE[key] = hit                 # most recently used last
        return hit[1:]
    if hit is not None:
        _image_cache_pixels -= _entry_cost(hit[1])

    xs, ys = project(positions, (0.0, 0.0, 0.0), scale, rotation, axes,
                     (0.0, 0.0), (0.0, 0.0), zoom, flip_y)
    left = int(math.floor(float(xs.min()))) - 1
    top = int(math.floor(float(ys.min()))) - 1
    width = int(math.ceil(float(xs.max()))) - left + 2
    height = int(math.ceil(float(ys.max()))) - top + 2
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        return None

    from PyQt5.QtGui import QColor, QImage, QPainter, QPen
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    segments = visible_segments(xs - left, ys - top, edges, width, height)
    if len(segments):
        painter = QPainter(image)
        painter.setPen(QPen(QColor(*colour), 1))
        painter.drawLines(segments_polygon(segments))
        painter.end()

    cost = _entry_cost(image)
    while _IMAGE_CACHE and (_image_cache_pixels + cost > IMAGE_CACHE_PIXELS
                            or len(_IMAGE_CACHE) >= IMAGE_CACHE_ENTRIES):
        old = _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
        _image_cache_pixels -= _entry_cost(old[1])
    _IMAGE_CACHE[key] = (model, image, left, top)
    _image_cache_pixels += cost
    return image, left, top


def _entry_cost(image):
    return image.width() * image.height() + IMAGE_ENTRY_COST


def clear_image_cache():
    """Drop every cached wireframe image (tests; a reloaded model)."""
    global _image_cache_pixels
    _IMAGE_CACHE.clear()
    _image_cache_pixels = 0
