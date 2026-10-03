"""The invariants every convex brush must satisfy, whatever produced it.

``engine.brush_geometry`` keeps a brush as a set of half-space planes and
*derives* everything else from it — corners, face windings, bounds, the render
mesh, the 2D silhouette, collision triangles.  The plane set is the source of
truth; the derived surface must never drift into being a second, independent
one.

These tests state that relationship directly rather than checking any one
operation's output:

* every corner satisfies every half-space (that is what "the solid is the
  intersection" *means*);
* every face winding lies on its own plane and is wound outward;
* the bounds contain the corners;
* recomputing produces the same solid;
* a translation moves the solid without changing its topology.

They are run over a catalogue of shapes — box, tetrahedron, clipped, scaled,
rotated, tiny, huge — so any of them failing points at the shape, not at a
one-off fixture.
"""

import math

import numpy as np
import pytest

from engine import brush_geometry as bg
from tests.helpers.worlds import corners_of, tetrahedron_planes


# ---------------------------------------------------------------------------
# Shape catalogue
# ---------------------------------------------------------------------------

def _clipped_box(scale=1.0):
    planes = bg.box_planes((0, 0, 0), (64 * scale, 64 * scale, 64 * scale))
    return bg.clip_planes(planes, np.array([1.0, 1.0, 0.0]) / math.sqrt(2),
                          20.0 * scale)


def _twice_clipped_box():
    planes = _clipped_box()
    return bg.clip_planes(planes, np.array([0.0, 1.0, 1.0]) / math.sqrt(2), 18.0)


def _wedge():
    """A very acute wedge: two faces meeting at a few degrees."""
    n = np.array([0.0, 1.0, 0.06])
    n = n / np.linalg.norm(n)
    return bg.clip_planes(bg.box_planes((0, 0, 0), (512, 64, 512)), n, 2.0)


SHAPES = {
    "box": lambda: bg.box_planes((0, 0, 0), (64, 64, 64)),
    "box_offcentre": lambda: bg.box_planes((300, -120, 75), (128, 16, 48)),
    "tetrahedron": tetrahedron_planes,
    "clipped_once": _clipped_box,
    "clipped_twice": _twice_clipped_box,
    "acute_wedge": _wedge,
    "tiny": lambda: bg.box_planes((0, 0, 0), (0.05, 0.05, 0.05)),
    "huge": lambda: bg.box_planes((0, 0, 0), (16384, 16384, 16384)),
    "thin_slab": lambda: bg.box_planes((0, 0, 0), (2048, 1.0, 2048)),
    "rotated": lambda: bg.rotate_planes(
        bg.box_planes((0, 0, 0), (64, 64, 64)), 37.0, (0.0, 1.0, 0.0), (0, 0, 0)),
    "rotated_offaxis": lambda: bg.rotate_planes(
        bg.box_planes((0, 0, 0), (96, 32, 64)), 23.0, (0.3, 1.0, -0.6), (12, 0, 5)),
    "scaled": lambda: bg.scale_planes(
        bg.box_planes((0, 0, 0), (64, 64, 64)), (3.0, 0.5, 1.25), (10.0, 0.0, -4.0)),
}

SHAPE_IDS = sorted(SHAPES)


@pytest.fixture(params=SHAPE_IDS)
def shape(request):
    """``(name, planes, ConvexGeometry)`` for one catalogue entry."""
    planes = SHAPES[request.param]()
    return request.param, planes, bg.ConvexGeometry(planes)


def _describe(name, convex):
    """Context every failure message in this module carries."""
    return ("shape=%s verts=%d faces=%d bounds=%s"
            % (name, len(convex.verts), len(convex.faces),
               tuple(np.round(np.concatenate(convex.bounds), 4))))


# ---------------------------------------------------------------------------
# Half-space containment — the defining property
# ---------------------------------------------------------------------------

def test_every_vertex_satisfies_every_half_space(shape):
    name, planes, convex = shape
    assert convex.is_valid, "%s produced no solid" % _describe(name, convex)
    normals, offsets = convex.plane_arrays()
    # Tolerance scales with the solid's size: a 16k-unit brush cannot be held to
    # the same absolute epsilon as a 0.05-unit one without asking float64 for
    # more than it has.  It is *not* loosened beyond that.
    span = float(np.max(convex.extents()))
    tol = bg.EPS * max(1.0, span)
    distances = convex.verts @ normals.T - offsets      # (V, P), >0 is outside
    worst = float(distances.max())
    if worst > tol:
        v, p = np.unravel_index(int(distances.argmax()), distances.shape)
        pytest.fail(
            "%s: vertex %d %s is %.6g outside plane %d (n=%s d=%.6g); "
            "tolerance %.6g"
            % (_describe(name, convex), v, tuple(np.round(convex.verts[v], 4)),
               worst, p, tuple(np.round(normals[p], 6)), offsets[p], tol))


def test_the_vertex_centroid_is_inside_the_solid(shape):
    """The average of the corners is interior for any convex solid.

    Deliberately *not* ``center()``: that is the AABB centre, which for a
    tetrahedron (or any solid whose bounding box it does not fill) is legally
    outside.  The centroid is the one point convexity guarantees.
    """
    name, planes, convex = shape
    centroid = convex.verts.mean(axis=0)
    assert convex.contains_point(centroid), (
        "%s: corner centroid %s is not inside its own solid"
        % (_describe(name, convex), tuple(np.round(centroid, 4))))


def test_the_aabb_centre_is_the_midpoint_of_the_bounds(shape):
    name, planes, convex = shape
    lo, hi = convex.bounds
    assert np.allclose(convex.center(), (lo + hi) * 0.5), (
        "%s: center() %s is not the midpoint of the bounds"
        % (_describe(name, convex), tuple(np.round(convex.center(), 4))))


def test_a_point_well_outside_is_not_contained(shape):
    name, planes, convex = shape
    lo, hi = convex.bounds
    outside = hi + (hi - lo) + 10.0
    assert not convex.contains_point(outside), (
        "%s: point %s beyond the bounds reported as inside"
        % (_describe(name, convex), tuple(np.round(outside, 4))))


# ---------------------------------------------------------------------------
# Faces and windings
# ---------------------------------------------------------------------------

def test_every_face_ring_lies_on_its_own_plane(shape):
    name, planes, convex = shape
    span = max(1.0, float(np.max(convex.extents())))
    tol = bg.EPS * span
    for face in convex.faces:
        plane = convex.planes[face["plane"]]
        n = np.asarray(plane["n"], dtype=float)
        n = n / np.linalg.norm(n)
        d = float(plane["d"])
        ring = convex.verts[face["indices"]]
        offsets = np.abs(ring @ n - d)
        assert offsets.max() <= tol, (
            "%s: face on plane %d has a corner %.6g off its plane "
            "(n=%s d=%.6g, tolerance %.6g)"
            % (_describe(name, convex), face["plane"], float(offsets.max()),
               tuple(np.round(n, 6)), d, tol))


def test_every_face_is_wound_outward(shape):
    """The ring's own normal must agree with the plane's outward normal.

    A face wound the other way renders back-to-front and collides inside-out,
    and nothing else in the pipeline re-checks it.
    """
    name, planes, convex = shape
    for face in convex.faces:
        ring = convex.verts[face["indices"]]
        ring_normal = bg._poly_normal(ring)
        declared = np.asarray(face["normal"], dtype=float)
        agreement = float(ring_normal @ declared)
        assert agreement > 0.9, (
            "%s: face on plane %d is wound inward (ring normal %s vs declared "
            "%s, dot=%.4f)"
            % (_describe(name, convex), face["plane"],
               tuple(np.round(ring_normal, 4)), tuple(np.round(declared, 4)),
               agreement))


def test_every_face_has_at_least_three_corners(shape):
    name, planes, convex = shape
    for face in convex.faces:
        assert len(face["indices"]) >= 3, (
            "%s: face on plane %d degenerated to %d corners"
            % (_describe(name, convex), face["plane"], len(face["indices"])))


def test_face_rings_have_no_repeated_corner(shape):
    name, planes, convex = shape
    for face in convex.faces:
        ring = face["indices"]
        assert len(set(ring)) == len(ring), (
            "%s: face on plane %d visits a corner twice: %s"
            % (_describe(name, convex), face["plane"], ring))


def test_every_edge_is_shared_by_exactly_two_faces(shape):
    """A closed solid has no boundary: each edge belongs to two faces.

    An edge used once means a hole; used three times means the windings
    disagree about the topology.  Either breaks collision and shadow volumes.
    """
    name, planes, convex = shape
    counts = {}
    for face in convex.faces:
        ring = face["indices"]
        for k in range(len(ring)):
            a, b = ring[k], ring[(k + 1) % len(ring)]
            key = (a, b) if a < b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    bad = {edge: n for edge, n in counts.items() if n != 2}
    assert not bad, (
        "%s: %d edge(s) not shared by exactly two faces: %s"
        % (_describe(name, convex), len(bad), sorted(bad.items())[:5]))


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_bounds_contain_every_vertex(shape):
    name, planes, convex = shape
    lo, hi = convex.bounds
    assert np.all(convex.verts >= lo - 1e-9) and np.all(convex.verts <= hi + 1e-9), (
        "%s: bounds %s .. %s do not contain the corners"
        % (_describe(name, convex), tuple(np.round(lo, 4)), tuple(np.round(hi, 4))))


def test_bounds_are_tight(shape):
    """Each bound must be attained by some corner, or culling over-draws."""
    name, planes, convex = shape
    lo, hi = convex.bounds
    for axis in range(3):
        assert np.isclose(convex.verts[:, axis].min(), lo[axis]), (
            "%s: lower bound on axis %d is %.6g but the nearest corner is %.6g"
            % (_describe(name, convex), axis, lo[axis],
               float(convex.verts[:, axis].min())))
        assert np.isclose(convex.verts[:, axis].max(), hi[axis]), (
            "%s: upper bound on axis %d is %.6g but the furthest corner is %.6g"
            % (_describe(name, convex), axis, hi[axis],
               float(convex.verts[:, axis].max())))


# ---------------------------------------------------------------------------
# Determinism and stability
# ---------------------------------------------------------------------------

def test_recomputation_is_bit_identical(shape):
    """Two builds from the same plane set must agree exactly.

    Not merely "close": the render mesh, collision mesh and cache signature are
    all keyed off this, so a build that wobbles would make the caches lie.
    """
    name, planes, convex = shape
    again = bg.ConvexGeometry(planes)
    assert np.array_equal(convex.verts, again.verts), (
        "%s: rebuilt corners differ (max delta %.6g)"
        % (_describe(name, convex),
           float(np.abs(convex.verts - again.verts).max())
           if convex.verts.shape == again.verts.shape else float("nan")))
    assert [f["indices"] for f in convex.faces] == [f["indices"] for f in again.faces], \
        "%s: rebuilt face windings differ" % _describe(name, convex)


def test_plane_order_does_not_change_the_solid(shape):
    """The solid is an intersection, so the plane list's order is irrelevant.

    Reversing it must give the same point set (face *numbering* changes with
    the order; the geometry must not).
    """
    name, planes, convex = shape
    reversed_convex = bg.ConvexGeometry(list(reversed(planes)))
    a = np.array(sorted(map(tuple, np.round(convex.verts, 6))))
    b = np.array(sorted(map(tuple, np.round(reversed_convex.verts, 6))))
    assert a.shape == b.shape and np.allclose(a, b, atol=1e-5), (
        "%s: reversing the plane list changed the corner set (%d vs %d corners)"
        % (_describe(name, convex), len(a), len(b)))


def test_translation_preserves_topology_and_shifts_every_corner(shape):
    name, planes, convex = shape
    delta = np.array([137.5, -42.25, 918.0])
    moved = bg.ConvexGeometry(bg.translate_planes(planes, delta))

    assert len(moved.verts) == len(convex.verts), (
        "%s: translation changed the corner count (%d -> %d)"
        % (_describe(name, convex), len(convex.verts), len(moved.verts)))
    assert [f["indices"] for f in moved.faces] == [f["indices"] for f in convex.faces], \
        "%s: translation changed the face windings" % _describe(name, convex)

    expected = np.array(sorted(map(tuple, np.round(convex.verts + delta, 4))))
    actual = np.array(sorted(map(tuple, np.round(moved.verts, 4))))
    assert np.allclose(expected, actual, atol=1e-3), (
        "%s: corners did not follow the translation (max error %.6g)"
        % (_describe(name, convex), float(np.abs(expected - actual).max())))


def test_translating_there_and_back_restores_the_solid(shape):
    name, planes, convex = shape
    delta = np.array([1000.0, -250.0, 37.5])
    there = bg.translate_planes(planes, delta)
    back = bg.ConvexGeometry(bg.translate_planes(there, -delta))
    assert np.allclose(back.verts, convex.verts, atol=1e-6), (
        "%s: round-trip translation drifted by %.6g"
        % (_describe(name, convex), float(np.abs(back.verts - convex.verts).max())))


# ---------------------------------------------------------------------------
# Derived representations agree with the solid
# ---------------------------------------------------------------------------

def test_triangulation_covers_every_face_and_stays_on_the_surface(shape):
    name, planes, convex = shape
    positions, normals, uvs = convex.triangulate()
    expected_tris = sum(len(f["indices"]) - 2 for f in convex.faces)
    assert len(positions) == expected_tris * 3, (
        "%s: fan triangulation produced %d vertices, expected %d (%d triangles)"
        % (_describe(name, convex), len(positions), expected_tris * 3, expected_tris))
    assert len(normals) == len(positions) and len(uvs) == len(positions), (
        "%s: triangulate() returned mismatched arrays (pos=%d nrm=%d uv=%d)"
        % (_describe(name, convex), len(positions), len(normals), len(uvs)))

    plane_normals, offsets = convex.plane_arrays()
    span = max(1.0, float(np.max(convex.extents())))
    # float32 mesh output against float64 planes: the tolerance is the float32
    # representation error at this scale, not a slackened epsilon.
    tol = max(bg.EPS, np.finfo(np.float32).eps * 8) * span
    worst = float((positions.astype(np.float64) @ plane_normals.T - offsets).max())
    assert worst <= tol, (
        "%s: a triangulated vertex sits %.6g outside the solid (tolerance %.6g)"
        % (_describe(name, convex), worst, tol))


def test_collision_triangles_match_the_triangulation(shape):
    name, planes, convex = shape
    tris = convex.collision_triangles()
    expected = sum(len(f["indices"]) - 2 for f in convex.faces)
    assert len(tris) == expected, (
        "%s: collision mesh has %d triangles, the surface has %d"
        % (_describe(name, convex), len(tris), expected))
    for (corners, normal) in tris:
        assert len(corners) == 3 and len(normal) == 3, (
            "%s: malformed collision triangle %r" % (_describe(name, convex),
                                                     (corners, normal)))


def test_silhouette_is_a_closed_hull_containing_the_projection(shape):
    """The 2D views draw this instead of a rectangle; it must enclose the brush."""
    name, planes, convex = shape
    for axis1, axis2 in ((0, 2), (0, 1), (1, 2)):
        hull = convex.silhouette(axis1, axis2)
        assert len(hull) >= 3, (
            "%s: silhouette on axes (%d,%d) has only %d points"
            % (_describe(name, convex), axis1, axis2, len(hull)))
        poly = np.asarray(hull)
        pts = convex.verts[:, [axis1, axis2]]
        # Every projected corner must be inside (or on) the hull.
        for point in pts:
            assert _point_in_convex_polygon(point, poly), (
                "%s: projected corner %s falls outside its own silhouette on "
                "axes (%d,%d)"
                % (_describe(name, convex), tuple(np.round(point, 4)), axis1, axis2))


#: ``_convex_hull_2d`` rounds its input to 4 decimals to dedup coincident
#: projections, so a hull vertex can sit up to 5e-5 inside the true corner.  The
#: containment check allows exactly that much and no more.
SILHOUETTE_QUANTISATION = 1e-4


def _point_in_convex_polygon(point, poly, eps=SILHOUETTE_QUANTISATION):
    """True when ``point`` is inside a CCW convex polygon, or within ``eps`` of it.

    ``eps`` is a *perpendicular distance* in world units, not a raw cross
    product, so the same tolerance means the same thing on a 1-unit brush and a
    16000-unit one.
    """
    n = len(poly)
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        length = math.hypot(ex, ey)
        if length < 1e-12:
            continue
        cross = ex * (point[1] - a[1]) - ey * (point[0] - a[0])
        if cross / length < -eps:
            return False
    return True


def test_edges_are_unique_ordered_pairs(shape):
    name, planes, convex = shape
    edges = convex.edges
    assert edges == sorted(set(edges)), (
        "%s: edge list is not a sorted unique set" % _describe(name, convex))
    for a, b in edges:
        assert a < b, "%s: edge (%d,%d) is not ordered" % (_describe(name, convex), a, b)
        assert 0 <= a < len(convex.verts) and 0 <= b < len(convex.verts), (
            "%s: edge (%d,%d) indexes outside the %d corners"
            % (_describe(name, convex), a, b, len(convex.verts)))


def test_euler_characteristic_of_a_closed_solid(shape):
    """V - E + F == 2 for any convex polyhedron.

    The single strongest statement that the corners, edges and faces describe
    one consistent closed solid rather than three separately-built lists.
    """
    name, planes, convex = shape
    v = len(convex.verts)
    e = len(convex.edges)
    f = len(convex.faces)
    assert v - e + f == 2, (
        "%s: V-E+F = %d-%d+%d = %d, not 2 - the surface is not a closed solid"
        % (_describe(name, convex), v, e, f, v - e + f))
