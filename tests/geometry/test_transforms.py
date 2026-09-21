"""Transforms and component rebuilds on the plane representation.

Every editor gesture that changes a brush's shape goes through one of these:
dragging moves the planes (``translate_brush``), a resize handle stretches them
(``fit_brush_to_bounds``), rotation turns them (``rotate_brush``), a vertex drag
re-derives them from the moved corners (``rebuild_brush_from_points``) and a
face drag slides one along its own normal (``offset_brush_planes``).

The thing they all have to get right is that the plane set stays the source of
truth: ``pos``/``size`` follow the geometry, an edit that would collapse the
solid is rejected outright rather than leaving half a brush, and the derived
surface is rebuilt rather than kept.
"""

import math

import numpy as np
import pytest

from engine import brush_geometry as bg
from tests.helpers.worlds import angled_brush, box_brush, corners_of


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def test_translate_brush_moves_planes_and_pos_together():
    brush = angled_brush("ramp")
    before = bg.get_convex(brush).center()
    delta = (128.0, -64.0, 32.0)

    assert bg.translate_brush(brush, delta) is True

    after = bg.get_convex(brush).center()
    assert after == pytest.approx(np.asarray(before) + np.asarray(delta)), (
        "geometry centre moved from %s to %s, expected %s"
        % (tuple(np.round(before, 3)), tuple(np.round(after, 3)),
           tuple(np.round(np.asarray(before) + np.asarray(delta), 3))))
    assert brush["pos"] == pytest.approx(list(after), abs=1e-3), (
        "pos %s drifted from the geometry centre %s - culling, the spatial "
        "grid and picking all read pos" % (brush["pos"], tuple(np.round(after, 3))))


def test_translate_brush_is_a_no_op_for_a_plain_box():
    """A box has no plane set to move; ``pos`` alone is its position."""
    brush = box_brush("b")
    assert bg.translate_brush(brush, (10, 0, 0)) is False
    assert brush["pos"] == [0.0, 0.0, 0.0], (
        "translate_brush moved a box brush's pos despite reporting no geometry "
        "to translate (pos=%s)" % (brush["pos"],))


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def test_rotating_a_box_by_ninety_degrees_swaps_its_extents():
    brush = box_brush("b", (0, 0, 0), (128, 32, 64))
    assert bg.rotate_brush(brush, 90.0, (0.0, 1.0, 0.0)) is True
    extents = bg.get_convex(brush).extents()
    assert extents == pytest.approx([64.0, 32.0, 128.0], abs=1e-6), (
        "a 90-degree Y rotation of a 128x32x64 brush should measure "
        "64x32x128, got %s" % (tuple(np.round(extents, 6)),))


def test_four_ninety_degree_rotations_return_the_original_solid():
    brush = box_brush("b", (0, 0, 0), (128, 32, 64))
    bg.box_to_geometry(brush)
    before = bg.get_convex(brush).verts.copy()
    for _ in range(4):
        bg.rotate_brush(brush, 90.0, (0.0, 1.0, 0.0))
    after = bg.get_convex(brush).verts
    a = np.array(sorted(map(tuple, np.round(before, 6))))
    b = np.array(sorted(map(tuple, np.round(after, 6))))
    assert np.allclose(a, b, atol=1e-6), (
        "four 90-degree rotations drifted by %.3g" % float(np.abs(a - b).max()))


def test_rotation_preserves_volume_measured_by_the_corner_cloud():
    """A rigid rotation cannot change any distance between corners."""
    brush = box_brush("b", (0, 0, 0), (128, 32, 64))
    bg.box_to_geometry(brush)
    before = bg.get_convex(brush).verts
    before_pairs = np.sort(np.linalg.norm(
        before[:, None, :] - before[None, :, :], axis=-1).ravel())

    bg.rotate_brush(brush, 37.0, (0.3, 1.0, -0.6))

    after = bg.get_convex(brush).verts
    after_pairs = np.sort(np.linalg.norm(
        after[:, None, :] - after[None, :, :], axis=-1).ravel())
    assert np.allclose(before_pairs, after_pairs, atol=1e-6), (
        "rotation changed the corner-to-corner distances by up to %.3g"
        % float(np.abs(before_pairs - after_pairs).max()))


def test_rotating_a_box_brush_promotes_it_to_geometry():
    brush = box_brush("b")
    assert "geometry" not in brush
    bg.rotate_brush(brush, 15.0, (0.0, 1.0, 0.0))
    assert bg.brush_has_geometry(brush), (
        "a rotated brush is no longer axis-aligned; it must carry a plane set")


def test_rotation_carries_the_texture_basis_with_the_face():
    """Without this the texture slides as the brush turns."""
    brush = box_brush("b")
    bg.rotate_brush(brush, 45.0, (0.0, 1.0, 0.0))
    for plane in brush["geometry"]["planes"]:
        assert plane.get("uv_u") is not None and plane.get("uv_v") is not None, (
            "plane %s came out of a rotation with no texture basis" % (plane["n"],))
        u = np.asarray(plane["uv_u"]); v = np.asarray(plane["uv_v"])
        n = np.asarray(plane["n"])
        assert abs(float(u @ n)) < 1e-6 and abs(float(v @ n)) < 1e-6, (
            "rotated texture basis is not tangent to its face "
            "(u.n=%.3g, v.n=%.3g)" % (float(u @ n), float(v @ n)))


# ---------------------------------------------------------------------------
# Fitting to new bounds (the resize handles)
# ---------------------------------------------------------------------------

def test_fit_to_bounds_makes_the_aabb_exactly_the_requested_box():
    brush = angled_brush("ramp", size=(64, 64, 64))
    assert bg.fit_brush_to_bounds(brush, (-100, 0, -50), (100, 40, 50)) is True
    lo, hi = bg.get_convex(brush).bounds
    assert lo == pytest.approx([-100, 0, -50], abs=1e-4), \
        "lower bound is %s, asked for (-100, 0, -50)" % (tuple(np.round(lo, 4)),)
    assert hi == pytest.approx([100, 40, 50], abs=1e-4), \
        "upper bound is %s, asked for (100, 40, 50)" % (tuple(np.round(hi, 4)),)


def test_fit_to_bounds_keeps_the_shape_convex_and_closed():
    brush = angled_brush("ramp")
    faces_before = len(bg.get_convex(brush).faces)
    bg.fit_brush_to_bounds(brush, (-200, -10, -200), (200, 10, 200))
    convex = bg.get_convex(brush)
    assert len(convex.faces) == faces_before, (
        "stretching changed the face count %d -> %d; it should only scale"
        % (faces_before, len(convex.faces)))
    assert len(convex.verts) - len(convex.edges) + len(convex.faces) == 2, \
        "stretched brush is no longer a closed solid"


def test_fit_to_bounds_is_a_no_op_for_a_box_brush():
    brush = box_brush("b")
    assert bg.fit_brush_to_bounds(brush, (-1, -1, -1), (1, 1, 1)) is False
    assert brush["size"] == [64.0, 64.0, 64.0]


# ---------------------------------------------------------------------------
# Component rebuilds (vertex / edge drags)
# ---------------------------------------------------------------------------

def test_moving_one_corner_rebuilds_the_hull_through_it():
    brush = box_brush("b")
    points = bg.brush_points(brush).copy()
    # Pull the +X+Y+Z corner outward.
    target = np.array([64.0, 64.0, 64.0])
    idx = int(np.argmax(points @ np.array([1.0, 1.0, 1.0])))
    points[idx] = target

    assert bg.rebuild_brush_from_points(brush, points) is True

    rebuilt = bg.brush_points(brush)
    distances = np.linalg.norm(rebuilt - target, axis=1)
    assert distances.min() < 1e-6, (
        "the moved corner %s is not in the rebuilt hull; nearest corner is "
        "%s (%.4g away)"
        % (tuple(target), tuple(np.round(rebuilt[int(distances.argmin())], 4)),
           float(distances.min())))
    assert bg.get_convex(brush).is_valid


def test_a_corner_move_that_would_flatten_the_brush_is_rejected():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    before = [dict(p) for p in brush["geometry"]["planes"]]

    flat = bg.brush_points(brush).copy()
    flat[:, 1] = 0.0                               # every corner onto one plane

    assert bg.rebuild_brush_from_points(brush, flat) is False, \
        "a point set with no volume must be rejected, not committed"
    assert brush["geometry"]["planes"] == before, \
        "a rejected rebuild must leave the brush byte-identical"


def test_a_rebuild_carries_face_textures_across():
    brush = box_brush("b")
    brush["textures"]["top"] = "Dev/ceiling.png"
    points = bg.brush_points(brush).copy()
    points[:, 1] *= 1.5                            # taller, same topology

    assert bg.rebuild_brush_from_points(brush, points) is True

    convex = bg.get_convex(brush)
    top = [f for f in convex.faces if f["normal"][1] > 0.99]
    assert len(top) == 1, "expected exactly one upward face, got %d" % len(top)
    assert top[0]["texture"] == "Dev/ceiling.png", (
        "the top face lost its texture in the rebuild (got %r)"
        % top[0]["texture"])


def test_too_many_corners_is_refused_rather_than_stalling_the_editor():
    """The hull search is O(n^3); past the cap the edit is rejected."""
    rng = np.random.default_rng(7)
    points = rng.normal(size=(bg.MAX_HULL_POINTS + 10, 3)) * 50.0
    assert bg.convex_hull_planes(points) == [], (
        "a %d-point cloud should be refused (cap is %d)"
        % (len(points), bg.MAX_HULL_POINTS))


def test_offsetting_a_face_plane_moves_only_that_face():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    convex = bg.get_convex(brush)
    top_index = next(i for i, p in enumerate(convex.planes) if p["n"][1] > 0.99)

    assert bg.offset_brush_planes(brush, {top_index: 100.0}) is True

    lo, hi = bg.get_convex(brush).bounds
    assert hi[1] == pytest.approx(100.0), \
        "the top face did not move to y=100 (top is %.4f)" % hi[1]
    assert lo[1] == pytest.approx(-32.0), \
        "the bottom face moved too (bottom is %.4f)" % lo[1]
    assert hi[0] == pytest.approx(32.0) and lo[0] == pytest.approx(-32.0), \
        "the X faces moved: %.4f .. %.4f" % (lo[0], hi[0])


def test_an_offset_that_would_invert_the_brush_is_rejected():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    convex = bg.get_convex(brush)
    top_index = next(i for i, p in enumerate(convex.planes) if p["n"][1] > 0.99)
    before = [dict(p) for p in brush["geometry"]["planes"]]

    # Push the top face below the bottom one.
    assert bg.offset_brush_planes(brush, {top_index: -100.0}) is False
    assert brush["geometry"]["planes"] == before, \
        "a rejected face offset must leave the plane set unchanged"


def test_an_offset_of_zero_reports_no_change():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    current = float(brush["geometry"]["planes"][0]["d"])
    assert bg.offset_brush_planes(brush, {0: current}) is False, \
        "offsetting a plane to the value it already has is not a change"


def test_plane_face_vertex_indices_names_the_corners_on_that_face():
    brush = box_brush("b")
    convex = bg.get_shape(brush)
    for index, plane in enumerate(convex.planes):
        indices = bg.plane_face_vertex_indices(brush, index)
        assert len(indices) == 4, (
            "box plane %d (n=%s) has %d corners, a box face has 4"
            % (index, tuple(plane["n"]), len(indices)))
        ring = bg.brush_points(brush)[indices]
        n = np.asarray(plane["n"], dtype=float)
        assert np.allclose(ring @ n, plane["d"], atol=1e-6), (
            "corners named for plane %d are not on it (distances %s)"
            % (index, np.round(ring @ n - plane["d"], 6).tolist()))


# ---------------------------------------------------------------------------
# Hull derivation
# ---------------------------------------------------------------------------

def test_the_hull_of_a_box_is_its_six_faces_at_the_right_offsets():
    planes = bg.convex_hull_planes(corners_of((10, 20, 30), (64, 32, 16)))
    assert len(planes) == 6, "expected 6 planes for a box, got %d" % len(planes)
    by_normal = {tuple(np.round(p["n"], 6)): p["d"] for p in planes}
    assert by_normal[(1.0, 0.0, 0.0)] == pytest.approx(42.0)
    assert by_normal[(-1.0, 0.0, 0.0)] == pytest.approx(22.0)
    assert by_normal[(0.0, 1.0, 0.0)] == pytest.approx(36.0)
    assert by_normal[(0.0, 0.0, 1.0)] == pytest.approx(38.0)


def test_interior_points_do_not_affect_the_hull():
    rng = np.random.default_rng(11)
    interior = rng.uniform(-20, 20, size=(12, 3))
    points = np.vstack([corners_of(), interior])
    planes = bg.convex_hull_planes(points)
    assert len(planes) == 6, (
        "12 interior points changed the hull of a box: %d planes" % len(planes))


def test_hull_normals_point_away_from_the_interior():
    points = corners_of((5, -5, 5), (40, 90, 20))
    centroid = points.mean(axis=0)
    for plane in bg.convex_hull_planes(points):
        n = np.asarray(plane["n"], dtype=float)
        assert float(n @ centroid) < plane["d"], (
            "plane n=%s d=%.4f does not contain the centroid %s - the normal "
            "points inward" % (tuple(np.round(n, 4)), plane["d"],
                               tuple(np.round(centroid, 4))))


def test_a_degenerate_point_set_yields_no_hull():
    assert bg.convex_hull_planes(np.zeros((3, 3))) == []
    assert bg.convex_hull_planes(np.zeros((0, 3))) == []
    collinear = np.array([[float(i), 0.0, 0.0] for i in range(10)])
    assert bg.convex_hull_planes(collinear) == [], \
        "points on a line enclose no volume"


def test_the_hull_of_a_rotated_box_still_has_six_faces():
    """Numerical robustness: rotation makes every coordinate irrational."""
    points = corners_of((0, 0, 0), (64, 64, 64))
    theta = math.radians(37.0)
    c, s = math.cos(theta), math.sin(theta)
    rot = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    planes = bg.convex_hull_planes(points @ rot.T)
    assert len(planes) == 6, (
        "the hull of a rotated box came out with %d planes, not 6" % len(planes))
