"""Deterministic world, brush and entity factories.

Fio's world *is* a pair of plain Python lists — brush dicts and ``Thing``
instances — shared by the editor, the logic thread, the renderer and plugins.
The factories here build that same representation, so a test never has to
invent a parallel one.  Everything is deterministic: no clocks, no ``random``,
no filesystem, and ids are derived from names rather than drawn from
``uuid4`` unless a test explicitly asks for real uuids.
"""

import uuid

import numpy as np

from engine import brush_geometry as bg


# ---------------------------------------------------------------------------
# Brushes
# ---------------------------------------------------------------------------

def box_brush(name="brush", pos=(0, 0, 0), size=(64, 64, 64), **props):
    """A plain axis-aligned box brush, the editor's default primitive.

    No ``geometry`` key: a box brush stays a box until something promotes it
    (:func:`engine.brush_geometry.box_to_geometry`), which is exactly what the
    editor does, so tests exercise the same two-representation path the real
    code has.
    """
    brush = {
        "id": stable_id(name),
        "name": name,
        "pos": [float(v) for v in pos],
        "size": [float(v) for v in size],
        "textures": {tag: "Dev/512.jpg" for tag in bg.FACE_TAGS},
    }
    brush.update(props)
    return brush


def angled_brush(name="angled", pos=(0, 0, 0), size=(64, 64, 64),
                 clip_normal=(1.0, 1.0, 0.0), clip_offset=None, **props):
    """A box brush that has been clipped once, so it carries a plane set."""
    brush = box_brush(name, pos, size, **props)
    n = np.asarray(clip_normal, dtype=float)
    n = n / np.linalg.norm(n)
    if clip_offset is None:
        # Cut through the brush rather than past it: a plane through a point a
        # quarter of the way out from the centre always bites.
        centre = np.asarray(pos, dtype=float)
        quarter = centre + n * (min(size) * 0.25)
        clip_offset = float(n @ quarter)
    assert bg.clip_brush(brush, n, clip_offset), (
        "clip_brush(%s) did not cut the brush - the fixture is wrong, not the code"
        % (name,))
    return brush


def stable_id(name):
    """A deterministic uuid derived from ``name``.

    Real ids are ``uuid4``; a test that asserts on ids wants them reproducible,
    and a test that asserts ids *change* (clone, duplicate) wants the before and
    after to be comparable.  Both work with a name-derived uuid5.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "fio-test://%s" % name))


def corners_of(pos=(0, 0, 0), size=(64, 64, 64)):
    """The eight corners of a box, as an ``(8, 3)`` array."""
    p = np.asarray(pos, dtype=float)
    h = np.asarray(size, dtype=float) * 0.5
    return np.array([[p[0] + sx * h[0], p[1] + sy * h[1], p[2] + sz * h[2]]
                     for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])


def tetrahedron_planes(scale=64.0):
    """Plane set of a tetrahedron — the smallest non-box convex solid.

    Three axis planes through the origin plus one cutting corner, which is a
    real solid with four triangular faces and four vertices: the minimum a
    convex-geometry builder has to handle.
    """
    s = float(scale)
    return [
        bg.make_plane((-1, 0, 0), (0, 0, 0)),
        bg.make_plane((0, -1, 0), (0, 0, 0)),
        bg.make_plane((0, 0, -1), (0, 0, 0)),
        bg.make_plane((1, 1, 1), (s, 0, 0)),
    ]


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def make_thing(cls, name, pos=(0, 0, 0), **props):
    """A ``Thing`` subclass instance with a fixed name and id.

    ``Thing.__init__`` draws a name from a class-level counter and an id from
    ``uuid4``; both are pinned here so a test can name an entity in an
    assertion and get the same thing every run.
    """
    properties = dict(props)
    properties["name"] = name
    properties.setdefault("id", stable_id(name))
    thing = cls(pos=[float(v) for v in pos], properties=properties)
    return thing


def world(brushes=(), things=()):
    """A ``(brushes, things)`` pair — Fio's whole world representation."""
    return list(brushes), list(things)


def level_data(brushes=(), things=(), **extra):
    """A map dict in the shape ``EditorState.load_from_data`` expects."""
    data = {
        "brushes": [dict(b) for b in brushes],
        "things": [t.to_dict() for t in things],
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# Whole scenes
# ---------------------------------------------------------------------------

def room(size=1024.0, wall=32.0, height=256.0, floor_y=0.0):
    """A closed box room: floor, ceiling and four walls.

    The standard stage for collision, line-of-sight and spatial-grid tests.
    Returned in a fixed order (floor, ceiling, north, south, east, west) so a
    test can index it without searching.
    """
    half = size * 0.5
    mid_y = floor_y + height * 0.5
    return [
        box_brush("floor", (0, floor_y - wall * 0.5, 0), (size, wall, size)),
        box_brush("ceiling", (0, floor_y + height + wall * 0.5, 0), (size, wall, size)),
        box_brush("north_wall", (0, mid_y, half + wall * 0.5), (size, height, wall)),
        box_brush("south_wall", (0, mid_y, -half - wall * 0.5), (size, height, wall)),
        box_brush("east_wall", (half + wall * 0.5, mid_y, 0), (wall, height, size)),
        box_brush("west_wall", (-half - wall * 0.5, mid_y, 0), (wall, height, size)),
    ]


def pillar_grid(count_x=4, count_z=4, spacing=256.0, size=(64, 256, 64)):
    """A regular grid of pillar brushes, for spatial and culling tests."""
    out = []
    for ix in range(count_x):
        for iz in range(count_z):
            out.append(box_brush(
                "pillar_%d_%d" % (ix, iz),
                ((ix - (count_x - 1) / 2.0) * spacing,
                 size[1] * 0.5,
                 (iz - (count_z - 1) / 2.0) * spacing),
                size))
    return out
