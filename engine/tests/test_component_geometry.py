"""Head-less tests for the component-editing geometry primitives.

These cover the plane-set maths that vertex/edge/face dragging is built on:
hull rebuilds from a moved corner, whole-plane offsets, the shape cache that
lets a plain box brush be picked without being promoted to geometry, and the
rejection of edits that would collapse a brush.  No GL context, no Qt.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine import brush_geometry as bg  # noqa: E402


def make_box(pos=(0, 0, 0), size=(64, 64, 64)):
    return {
        'pos': list(pos),
        'size': list(size),
        'textures': {tag: 'tex_%s.png' % tag for tag in bg.FACE_TAGS},
    }


def box_corners(pos=(0, 0, 0), size=(64, 64, 64)):
    p = np.asarray(pos, dtype=float)
    h = np.asarray(size, dtype=float) / 2.0
    return np.array([[p[0] + sx * h[0], p[1] + sy * h[1], p[2] + sz * h[2]]
                     for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])


# ---------------------------------------------------------------------------
# convex_hull_planes
# ---------------------------------------------------------------------------

def test_hull_of_a_box_is_six_axis_planes():
    planes = bg.convex_hull_planes(box_corners())
    assert len(planes) == 6
    normals = sorted(tuple(p['n']) for p in planes)
    assert normals == sorted([
        (-1.0, 0.0, 0.0), (1.0, 0.0, 0.0),
        (0.0, -1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, -1.0), (0.0, 0.0, 1.0),
    ])
    for plane in planes:
        assert abs(plane['d']) == pytest.approx(32.0)


def test_hull_normals_point_outward():
    planes = bg.convex_hull_planes(box_corners())
    centre = np.zeros(3)
    for plane in planes:
        # The inside convention is dot(n, p) <= d, so the centre must satisfy it.
        assert float(np.asarray(plane['n']) @ centre) < plane['d']


def test_hull_ignores_interior_points():
    points = np.vstack([box_corners(), np.zeros((1, 3)), np.array([[5.0, 5.0, 5.0]])])
    planes = bg.convex_hull_planes(points)
    assert len(planes) == 6


def test_hull_rejects_degenerate_point_sets():
    assert bg.convex_hull_planes(np.zeros((3, 3))) == []
    # All eight points on one plane: no volume, so no hull.
    flat = box_corners()
    flat[:, 1] = 0.0
    assert bg.convex_hull_planes(flat) == []


def test_hull_refuses_absurd_point_counts():
    rng = np.random.default_rng(0)
    too_many = rng.normal(size=(bg.MAX_HULL_POINTS + 1, 3)) * 100.0
    assert bg.convex_hull_planes(too_many) == []


# ---------------------------------------------------------------------------
# rebuild_brush_from_points  (vertex / edge / shear drags)
# ---------------------------------------------------------------------------

def test_rebuild_from_moved_corner_adds_a_cut_face():
    brush = make_box()
    points = bg.brush_points(brush).copy()
    index = int(np.argmin(np.linalg.norm(points - np.array([32.0, 32.0, 32.0]), axis=1)))
    points[index] += np.array([0.0, -32.0, 0.0])
    assert bg.rebuild_brush_from_points(brush, points) is True
    convex = bg.get_convex(brush)
    assert convex.is_valid
    assert len(convex.faces) == 7          # six box sides plus the new cut


def test_rebuild_keeps_face_textures():
    """A corner pulled inward keeps every box side, textures and all."""
    brush = make_box()
    points = bg.brush_points(brush).copy()
    index = int(np.argmin(np.linalg.norm(points - np.array([32.0, 32.0, 32.0]), axis=1)))
    points[index] += np.array([0.0, -16.0, 0.0])
    assert bg.rebuild_brush_from_points(brush, points)
    faces = bg.get_convex(brush).faces
    textures = {f.get('face'): f.get('texture') for f in faces}
    for tag in ('east', 'west', 'top', 'down', 'north', 'south'):
        assert textures.get(tag) == 'tex_%s.png' % tag
    # The face the drag created has no box tag, but it is still textured.
    cut = [f for f in faces if not f.get('face')]
    assert len(cut) == 1
    assert cut[0].get('texture')


def test_rebuild_drops_a_face_the_drag_really_removed():
    """Pushing a corner outward replaces the flat side it belonged to."""
    brush = make_box()
    points = bg.brush_points(brush).copy()
    index = int(np.argmin(np.linalg.norm(points - np.array([32.0, -32.0, 32.0]), axis=1)))
    points[index] += np.array([0.0, -24.0, 0.0])       # spike the corner down
    assert bg.rebuild_brush_from_points(brush, points)
    tags = {f.get('face') for f in bg.get_convex(brush).faces}
    assert 'down' not in tags                          # no flat bottom left
    assert 'top' in tags
    assert brush['size'][1] == pytest.approx(88.0)


def test_rebuild_hands_each_box_tag_out_once():
    brush = make_box()
    points = bg.brush_points(brush).copy()
    points[0] += np.array([0.0, -16.0, 0.0])
    assert bg.rebuild_brush_from_points(brush, points)
    tags = [f['face'] for f in bg.get_convex(brush).faces if f.get('face')]
    assert len(tags) == len(set(tags))


def test_rebuild_rejects_a_collapse_and_leaves_the_brush_alone():
    brush = make_box()
    before = [dict(p) for p in bg.box_planes(brush['pos'], brush['size'])]
    flat = bg.brush_points(brush).copy()
    flat[:, 1] = 0.0                       # squash the brush to a sheet
    assert bg.rebuild_brush_from_points(brush, flat) is False
    assert 'geometry' not in brush
    assert brush['size'] == [64, 64, 64]
    assert len(before) == 6


def test_rebuild_result_is_always_convex():
    """Pulling a corner far past its neighbours still yields a convex solid."""
    brush = make_box()
    points = bg.brush_points(brush).copy()
    points[0] += np.array([200.0, 150.0, -120.0])
    assert bg.rebuild_brush_from_points(brush, points)
    convex = bg.get_convex(brush)
    normals, offsets = convex.plane_arrays()
    # Every corner must satisfy every half-space: that is what convex means.
    assert np.all(convex.verts @ normals.T - offsets <= 1e-3)


# ---------------------------------------------------------------------------
# offset_brush_planes  (side stretch / face drag)
# ---------------------------------------------------------------------------

def test_offset_plane_moves_one_side_only():
    brush = make_box()
    bg.box_to_geometry(brush)
    planes = brush['geometry']['planes']
    east = next(i for i, p in enumerate(planes) if p['n'] == [1.0, 0.0, 0.0])
    assert bg.offset_brush_planes(brush, {east: planes[east]['d'] + 32.0})
    assert brush['size'][0] == pytest.approx(96.0)
    assert brush['size'][1] == pytest.approx(64.0)
    assert brush['size'][2] == pytest.approx(64.0)
    assert brush['pos'][0] == pytest.approx(16.0)


def test_offset_plane_rejects_a_collapse():
    brush = make_box()
    bg.box_to_geometry(brush)
    planes = brush['geometry']['planes']
    east = next(i for i, p in enumerate(planes) if p['n'] == [1.0, 0.0, 0.0])
    assert bg.offset_brush_planes(brush, {east: -1000.0}) is False
    assert brush['size'] == [64, 64, 64]


def test_offset_plane_is_a_no_op_without_movement():
    brush = make_box()
    bg.box_to_geometry(brush)
    d = brush['geometry']['planes'][0]['d']
    assert bg.offset_brush_planes(brush, {0: d}) is False


def test_offset_plane_works_on_an_angled_brush():
    """The sloped face of a clipped brush can be slid like any other side."""
    brush = make_box()
    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)      # make it a wedge
    index = len(brush['geometry']['planes']) - 1           # the cut plane
    before = np.sort(bg.get_convex(brush).verts, axis=0)
    assert bg.offset_brush_planes(
        brush, {index: brush['geometry']['planes'][index]['d'] + 16.0})
    convex = bg.get_convex(brush)
    assert convex.is_valid
    after = np.sort(convex.verts, axis=0)
    assert before.shape != after.shape or not np.allclose(before, after)
    assert brush['geometry']['planes'][index]['d'] == pytest.approx(16.0)


# ---------------------------------------------------------------------------
# get_shape / edges  (picking against a plain box brush)
# ---------------------------------------------------------------------------

def test_shape_of_a_box_brush_does_not_promote_it_to_geometry():
    brush = make_box()
    shape = bg.get_shape(brush)
    assert shape is not None and shape.is_valid
    assert len(shape.verts) == 8
    assert len(shape.faces) == 6
    assert 'geometry' not in brush         # still a plain box on disk


def test_shape_cache_is_reused_and_follows_resizes():
    brush = make_box()
    first = bg.get_shape(brush)
    assert bg.get_shape(brush) is first    # cached, not rebuilt
    brush['size'] = [128, 64, 64]
    second = bg.get_shape(brush)
    assert second is not first
    assert second.extents()[0] == pytest.approx(128.0)


def test_shape_cache_keys_are_runtime_only():
    assert '_box_shape' in bg.GEO_RUNTIME_KEYS
    assert '_box_shape_sig' in bg.GEO_RUNTIME_KEYS


def test_geometry_invalidation_drops_the_box_shape_cache():
    brush = make_box()
    bg.get_shape(brush)
    assert '_box_shape' in brush
    bg.box_to_geometry(brush)              # calls _invalidate internally
    assert '_box_shape' not in brush


def test_box_has_twelve_edges():
    brush = make_box()
    edges = bg.get_shape(brush).edges
    assert len(edges) == 12
    assert all(a < b for a, b in edges)
    assert len(set(edges)) == 12


def test_edges_are_cached_per_geometry():
    shape = bg.get_shape(make_box())
    assert shape.edges is shape.edges


def test_plane_face_vertex_indices_returns_a_ring():
    brush = make_box()
    bg.box_to_geometry(brush)
    ring = bg.plane_face_vertex_indices(brush, 0)
    assert len(ring) == 4
    assert len(set(ring)) == 4


# ---------------------------------------------------------------------------
# rotate_point  (entities orbiting a free-rotate pivot)
# ---------------------------------------------------------------------------

def test_rotate_point_turns_about_the_pivot():
    out = bg.rotate_point([64.0, 0.0, 0.0], 90.0, [0.0, 1.0, 0.0], [0.0, 0.0, 0.0])
    assert out[0] == pytest.approx(0.0, abs=1e-9)
    assert out[1] == pytest.approx(0.0, abs=1e-9)
    assert abs(out[2]) == pytest.approx(64.0)


def test_rotate_point_leaves_the_pivot_itself_alone():
    pivot = [17.0, -3.0, 42.0]
    out = bg.rotate_point(pivot, 37.0, [0.0, 1.0, 0.0], pivot)
    assert out == pytest.approx(pivot)


def test_rotate_point_matches_how_a_brush_rotates():
    """A point and a brush corner at the same place must end up together.

    This is what keeps a mixed brush/entity selection rigid while it spins.
    """
    pivot = [0.0, 0.0, 0.0]
    axis = [0.0, 1.0, 0.0]
    brush = make_box(pos=(128, 0, 0))
    corner = bg.brush_points(brush)[0].tolist()
    assert bg.rotate_brush(brush, 33.0, axis, pivot=pivot)
    moved_corner = bg.rotate_point(corner, 33.0, axis, pivot)
    assert any(np.allclose(p, moved_corner, atol=1e-6)
               for p in bg.brush_points(brush))


def test_rotate_point_is_reversible():
    start = [12.0, 5.0, -30.0]
    pivot = [1.0, 2.0, 3.0]
    axis = [0.2, 1.0, -0.4]
    there = bg.rotate_point(start, 41.0, axis, pivot)
    back = bg.rotate_point(there, -41.0, axis, pivot)
    assert back == pytest.approx(start, abs=1e-9)
