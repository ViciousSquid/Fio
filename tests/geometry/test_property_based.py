"""Generated convex solids, checked against the geometry invariants.

Hand-written shapes cover the cases someone thought of.  These generate plane
sets and point clouds from a fixed list of seeds and check that the *same*
invariants hold — corners inside every half-space, closed surface, bounds
tight, clipping never leaks geometry outside the cut.

Seeds are explicit and enumerated, never drawn from the clock, so a failure
reproduces from its test id (``...[seed17]``) and the suite's runtime does not
vary between runs.
"""

import math

import numpy as np
import pytest

from engine import brush_geometry as bg


#: The generated cases.  A fixed list rather than a count so adding one is a
#: deliberate act and the ids stay stable across runs.
SEEDS = [1, 2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]


def _random_convex_planes(rng, count=None, radius=None):
    """A plane set guaranteed to bound a non-empty solid.

    Two properties have to hold for the generated case to be a fair test.

    *Non-empty*: every random plane is tangent to a sphere of ``radius`` about
    the origin, so the whole sphere is inside all of them however the normals
    fall.

    *Bounded*: a handful of half-spaces generally does **not** enclose a finite
    region, and an unbounded one is not a brush - ``compute_windings`` would
    close it against its internal ``_BOGUS`` seed polygon and hand back a
    262144-unit artefact.  A box of six axis planes at ``2 * radius`` is always
    present to close it, which is also what a real Fio brush looks like: a box
    that clips have been added to.
    """
    count = count or int(rng.integers(3, 10))
    radius = radius or float(rng.uniform(8.0, 400.0))
    planes = list(bg.box_planes((0, 0, 0), (radius * 4,) * 3))
    for _ in range(count):
        n = rng.normal(size=3)
        norm = float(np.linalg.norm(n))
        if norm < 1e-9:
            continue
        n = n / norm
        # d = radius keeps the plane tangent to the sphere of that radius,
        # so the origin (and the whole sphere) stays inside.
        planes.append(bg.make_plane(n, n * radius))
    return planes


def _random_point_cloud(rng, count=None, scale=None):
    count = count or int(rng.integers(5, 25))
    scale = scale or float(rng.uniform(4.0, 900.0))
    return rng.normal(size=(count, 3)) * scale


@pytest.fixture(params=SEEDS, ids=lambda s: "seed%d" % s)
def rng(request):
    return np.random.default_rng(request.param)


def _fail_context(planes, convex):
    return ("%d planes, %d verts, %d faces; normals=%s"
            % (len(planes), len(convex.verts), len(convex.faces),
               [tuple(np.round(p["n"], 3)) for p in planes[:6]]))


# ---------------------------------------------------------------------------
# Generated plane sets
# ---------------------------------------------------------------------------

def test_generated_plane_sets_enclose_a_valid_solid(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    assert convex.is_valid, (
        "a set of planes all tangent to a sphere must enclose it: %s"
        % _fail_context(planes, convex))


def test_generated_solids_satisfy_every_half_space(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    normals, offsets = convex.plane_arrays()
    tol = bg.EPS * max(1.0, float(np.max(convex.extents())))
    worst = float((convex.verts @ normals.T - offsets).max())
    assert worst <= tol, (
        "a generated corner sits %.6g outside its own half-space (tol %.6g); %s"
        % (worst, tol, _fail_context(planes, convex)))


def test_generated_solids_are_closed(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    v, e, f = len(convex.verts), len(convex.edges), len(convex.faces)
    assert v - e + f == 2, (
        "generated solid is not closed: V-E+F = %d-%d+%d = %d; %s"
        % (v, e, f, v - e + f, _fail_context(planes, convex)))


def test_generated_solids_have_tight_bounds(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    lo, hi = convex.bounds
    assert np.allclose(convex.verts.min(axis=0), lo)
    assert np.allclose(convex.verts.max(axis=0), hi)


def test_generated_solids_rebuild_identically(rng):
    planes = _random_convex_planes(rng)
    first = bg.ConvexGeometry(planes)
    second = bg.ConvexGeometry(planes)
    assert np.array_equal(first.verts, second.verts), (
        "two builds of the same generated plane set disagree; %s"
        % _fail_context(planes, first))


# ---------------------------------------------------------------------------
# Generated clip sequences
# ---------------------------------------------------------------------------

def test_a_random_clip_sequence_never_leaks_geometry_outside_a_cut(rng):
    """The single most important clipping property, over generated cuts."""
    brush = {"pos": [0.0, 0.0, 0.0], "size": [256.0, 256.0, 256.0],
             "name": "fuzz", "textures": {tag: "t.png" for tag in bg.FACE_TAGS}}
    applied = []
    for step in range(6):
        n = rng.normal(size=3)
        norm = float(np.linalg.norm(n))
        if norm < 1e-9:
            continue
        n = n / norm
        d = float(rng.uniform(-40.0, 120.0))
        if not bg.clip_brush(brush, n, d):
            continue
        applied.append((n, d))
        convex = bg.get_convex(brush)
        assert convex.is_valid, (
            "brush became degenerate after %d accepted cuts (last n=%s d=%.4f)"
            % (len(applied), tuple(np.round(n, 4)), d))
        for prev_n, prev_d in applied:
            overshoot = float((convex.verts @ prev_n - prev_d).max())
            assert overshoot <= bg.EPS * 100, (
                "after cut %d a corner sits %.6g outside an earlier cut "
                "(n=%s d=%.4f); %d corners"
                % (step, overshoot, tuple(np.round(prev_n, 4)), prev_d,
                   len(convex.verts)))


def test_a_random_clip_sequence_keeps_pos_in_step_with_the_geometry(rng):
    brush = {"pos": [0.0, 0.0, 0.0], "size": [256.0, 256.0, 256.0],
             "name": "fuzz", "textures": {tag: "t.png" for tag in bg.FACE_TAGS}}
    for _ in range(5):
        n = rng.normal(size=3)
        n = n / max(float(np.linalg.norm(n)), 1e-9)
        bg.clip_brush(brush, n, float(rng.uniform(0.0, 100.0)))
    convex = bg.get_convex(brush)
    lo, hi = convex.bounds
    assert brush["pos"] == pytest.approx(list((lo + hi) * 0.5), abs=1e-3), (
        "pos %s is not the centre of the geometry bounds %s..%s"
        % (brush["pos"], tuple(np.round(lo, 3)), tuple(np.round(hi, 3))))


# ---------------------------------------------------------------------------
# Generated point clouds (the component-edit path)
# ---------------------------------------------------------------------------

def test_the_hull_of_a_point_cloud_contains_every_point(rng):
    points = _random_point_cloud(rng)
    planes = bg.convex_hull_planes(points)
    if not planes:
        pytest.skip("this cloud is degenerate (no volume) - nothing to contain")
    normals = np.array([p["n"] for p in planes], dtype=float)
    offsets = np.array([p["d"] for p in planes], dtype=float)
    scale = float(np.abs(points).max())
    tol = bg.EPS * max(1.0, scale)
    worst = float((points @ normals.T - offsets).max())
    assert worst <= tol, (
        "the hull excludes one of its own points by %.6g (tol %.6g); "
        "%d points, %d planes" % (worst, tol, len(points), len(planes)))


def test_the_hull_of_a_point_cloud_is_a_closed_solid(rng):
    points = _random_point_cloud(rng)
    planes = bg.convex_hull_planes(points)
    if not planes:
        pytest.skip("degenerate cloud")
    convex = bg.ConvexGeometry(planes)
    v, e, f = len(convex.verts), len(convex.edges), len(convex.faces)
    assert v - e + f == 2, (
        "hull of %d generated points is not closed: V-E+F = %d-%d+%d = %d"
        % (len(points), v, e, f, v - e + f))


def test_translating_a_generated_solid_preserves_its_topology(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    delta = rng.uniform(-5000, 5000, size=3)
    moved = bg.ConvexGeometry(bg.translate_planes(planes, delta))
    assert len(moved.verts) == len(convex.verts), (
        "translation by %s changed the corner count %d -> %d"
        % (tuple(np.round(delta, 1)), len(convex.verts), len(moved.verts)))
    assert [f["indices"] for f in moved.faces] == [f["indices"] for f in convex.faces], \
        "translation by %s changed the windings" % (tuple(np.round(delta, 1)),)


def test_scaling_a_generated_solid_scales_its_extents(rng):
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    factor = np.array([2.0, 0.5, 3.0])
    scaled = bg.ConvexGeometry(bg.scale_planes(planes, factor, (0.0, 0.0, 0.0)))
    expected = convex.extents() * factor
    assert scaled.extents() == pytest.approx(expected, rel=1e-6), (
        "extents %s after scaling by %s, expected %s"
        % (tuple(np.round(scaled.extents(), 4)), tuple(factor),
           tuple(np.round(expected, 4))))


def test_containment_agrees_with_the_half_space_test_for_random_probes(rng):
    """``contains_point`` must not disagree with the plane arithmetic."""
    planes = _random_convex_planes(rng)
    convex = bg.ConvexGeometry(planes)
    normals, offsets = convex.plane_arrays()
    lo, hi = convex.bounds
    span = hi - lo
    probes = rng.uniform(lo - span * 0.5, hi + span * 0.5, size=(64, 3))
    for probe in probes:
        expected = bool(np.all(normals @ probe - offsets <= bg.EPS))
        actual = convex.contains_point(probe)
        assert actual == expected, (
            "contains_point(%s) returned %r but the half-space test says %r "
            "(max signed distance %.6g)"
            % (tuple(np.round(probe, 3)), actual, expected,
               float((normals @ probe - offsets).max())))
