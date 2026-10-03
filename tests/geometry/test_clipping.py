"""Clipping: the operation that turns a box into an arbitrary convex brush.

Clipping in Fio is *appending a plane*.  Everything that makes that safe is
here: a cut that would empty the brush is refused, a cut the brush does not
reach is refused (it would bound nothing and cost O(planes^2) on every rebuild
for ever), a cut the brush already has is refused (it would double the surface),
and whatever survives is still a valid convex solid.

The strongest statement in this module is that clipping *never* produces
geometry outside the kept half-space.  That is the property the clip tool's
correctness rests on, and it is checked after every clip, including repeated
ones.
"""

import math

import numpy as np
import pytest

from engine import brush_geometry as bg
from tests.helpers.worlds import box_brush


UNIT_DIAGONAL = np.array([1.0, 1.0, 0.0]) / math.sqrt(2.0)


def _outside_distance(convex, normal, offset):
    """How far the furthest corner pokes past ``dot(n, p) <= d``."""
    n = np.asarray(normal, dtype=float)
    n = n / np.linalg.norm(n)
    return float((convex.verts @ n - float(offset)).max())


# ---------------------------------------------------------------------------
# What a clip does
# ---------------------------------------------------------------------------

def test_clipping_a_box_appends_exactly_one_plane():
    planes = bg.box_planes((0, 0, 0), (64, 64, 64))
    clipped = bg.clip_planes(planes, UNIT_DIAGONAL, 20.0)
    assert len(clipped) == len(planes) + 1, (
        "a clip must append exactly one plane: %d -> %d"
        % (len(planes), len(clipped)))
    assert [p["n"] for p in clipped[:-1]] == [p["n"] for p in planes], \
        "the original planes must be carried through unchanged"


def test_no_geometry_survives_outside_the_cut_plane():
    """The defining property of a clip."""
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    bg.clip_brush(brush, UNIT_DIAGONAL, 20.0)
    convex = bg.get_convex(brush)
    overshoot = _outside_distance(convex, UNIT_DIAGONAL, 20.0)
    assert overshoot <= bg.EPS, (
        "clipped brush has a corner %.6g outside the cut plane "
        "(n=%s d=20.0, tolerance %.6g); corners=%s"
        % (overshoot, tuple(np.round(UNIT_DIAGONAL, 4)), bg.EPS,
           np.round(convex.verts, 3).tolist()))


def test_keep_positive_keeps_the_other_half():
    lower = bg.ConvexGeometry(
        bg.clip_planes(bg.box_planes((0, 0, 0), (64, 64, 64)), (0, 1, 0), 0.0))
    upper = bg.ConvexGeometry(
        bg.clip_planes(bg.box_planes((0, 0, 0), (64, 64, 64)), (0, 1, 0), 0.0,
                       keep_positive=True))
    assert lower.bounds[1][1] == pytest.approx(0.0), (
        "the default half must be the one below the plane; got top y=%.4f"
        % lower.bounds[1][1])
    assert upper.bounds[0][1] == pytest.approx(0.0), (
        "keep_positive must keep the half above the plane; got bottom y=%.4f"
        % upper.bounds[0][1])
    # Together they are the original brush and they do not overlap.
    assert lower.bounds[0][1] == pytest.approx(-32.0)
    assert upper.bounds[1][1] == pytest.approx(32.0)


def test_the_cut_face_is_a_real_face_of_the_result():
    planes = bg.clip_planes(bg.box_planes((0, 0, 0), (64, 64, 64)),
                            UNIT_DIAGONAL, 20.0)
    convex = bg.ConvexGeometry(planes)
    cut_index = len(planes) - 1
    cut_faces = [f for f in convex.faces if f["plane"] == cut_index]
    assert len(cut_faces) == 1, (
        "the cut plane must contribute exactly one face, got %d "
        "(faces on planes %s)"
        % (len(cut_faces), [f["plane"] for f in convex.faces]))
    assert len(cut_faces[0]["indices"]) >= 3


def test_the_cut_face_inherits_a_texture_from_the_brush():
    """A new face with no texture renders untextured; it must inherit one."""
    planes = bg.box_planes((0, 0, 0), (64, 64, 64),
                           textures={tag: "Dev/grid.png" for tag in bg.FACE_TAGS})
    clipped = bg.clip_planes(planes, UNIT_DIAGONAL, 20.0)
    assert clipped[-1]["texture"] == "Dev/grid.png", (
        "cut face texture is %r, expected it to inherit 'Dev/grid.png'"
        % (clipped[-1]["texture"],))


# ---------------------------------------------------------------------------
# Clips that must be refused
# ---------------------------------------------------------------------------

def test_a_clip_that_misses_the_brush_changes_nothing():
    """A plane the brush does not reach bounds nothing.

    Keeping it would grow the plane set on every missed clip, and every winding
    rebuild is O(planes^2), so the brush would carry that cost for life.
    """
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    before = bg.get_convex(bg.box_to_geometry(brush))
    plane_count = len(brush["geometry"]["planes"])

    cut = bg.clip_brush(brush, (1, 0, 0), 1000.0)

    assert cut is False, "clip_brush reported a cut for a plane 1000 units away"
    assert len(brush["geometry"]["planes"]) == plane_count, (
        "a missed clip added a plane: %d -> %d"
        % (plane_count, len(brush["geometry"]["planes"])))
    after = bg.get_convex(brush)
    assert np.array_equal(before.verts, after.verts), \
        "a missed clip changed the brush's corners"


def test_a_clip_that_would_empty_the_brush_is_refused():
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    bg.box_to_geometry(brush)
    planes_before = [dict(p) for p in brush["geometry"]["planes"]]

    cut = bg.clip_brush(brush, (1, 0, 0), -1000.0)

    assert cut is False, "clip_brush reported a cut that would delete the solid"
    assert brush["geometry"]["planes"] == planes_before, \
        "a refused clip must leave the plane set byte-identical"
    assert bg.get_convex(brush).is_valid, "the brush must still be a solid"


def test_repeating_the_same_clip_does_not_duplicate_the_plane():
    """Two coincident planes describe one half-space but make two faces.

    That means doubled collision triangles, doubled draw calls and z-fighting
    between two copies of one surface.
    """
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    assert bg.clip_brush(brush, UNIT_DIAGONAL, 20.0) is True
    count_after_first = len(brush["geometry"]["planes"])

    assert bg.clip_brush(brush, UNIT_DIAGONAL, 20.0) is False, \
        "clipping with a plane the brush already has must report no cut"
    assert len(brush["geometry"]["planes"]) == count_after_first, (
        "the duplicate clip added a plane: %d -> %d"
        % (count_after_first, len(brush["geometry"]["planes"])))

    convex = bg.get_convex(brush)
    normals = [tuple(np.round(p["n"], 6)) for p in convex.planes]
    assert len(normals) == len(set(normals)), \
        "the plane set contains duplicate normals: %s" % (normals,)


def test_a_clip_coincident_with_an_existing_box_face_is_refused():
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    bg.box_to_geometry(brush)
    assert bg.clip_brush(brush, (1, 0, 0), 32.0) is False, (
        "clipping exactly along the brush's own +X face must be a no-op")
    assert len(brush["geometry"]["planes"]) == 6


# ---------------------------------------------------------------------------
# Repeated clipping
# ---------------------------------------------------------------------------

REPEATED_CUTS = [
    ((1.0, 1.0, 0.0), 22.0),
    ((0.0, 1.0, 1.0), 20.0),
    ((-1.0, 1.0, 0.0), 21.0),
    ((1.0, 0.0, 1.0), 19.0),
    ((-1.0, -1.0, -1.0), 14.0),
    ((0.5, 1.0, -0.25), 17.0),
]


def test_repeated_clipping_keeps_the_brush_valid_and_inside_every_cut():
    brush = box_brush("b", (0, 0, 0), (128, 128, 128))
    applied = []
    for step, (normal, offset) in enumerate(REPEATED_CUTS):
        n = np.asarray(normal, dtype=float)
        n = n / np.linalg.norm(n)
        if not bg.clip_brush(brush, n, offset):
            continue  # a later cut can legitimately miss the shrunken brush
        applied.append((n, offset))
        convex = bg.get_convex(brush)
        assert convex.is_valid, (
            "brush became degenerate after cut %d (n=%s d=%s): %d verts, %d faces"
            % (step, tuple(np.round(n, 4)), offset, len(convex.verts),
               len(convex.faces)))
        # Every cut applied so far must still hold.
        for prev_n, prev_d in applied:
            overshoot = _outside_distance(convex, prev_n, prev_d)
            assert overshoot <= bg.EPS * 10, (
                "after cut %d the brush pokes %.6g outside an earlier cut "
                "(n=%s d=%s)"
                % (step, overshoot, tuple(np.round(prev_n, 4)), prev_d))
    assert len(applied) >= 4, (
        "the fixture is meant to land several cuts; only %d applied" % len(applied))


def test_every_accepted_clip_contributes_a_face_at_the_moment_it_lands():
    """``clip_brush`` returns True only for a plane that actually bounds the solid.

    That is the guarantee it documents, and it is what stops a run of missed
    clips from growing the plane set — every winding rebuild is quadratic in the
    plane count.  (A plane can *later* stop contributing when a subsequent cut
    shaves its face away; Fio keeps those, see
    ``test_the_plane_set_grows_only_by_accepted_cuts``.)
    """
    brush = box_brush("b", (0, 0, 0), (128, 128, 128))
    for step, (normal, offset) in enumerate(REPEATED_CUTS):
        n = np.asarray(normal, dtype=float)
        n = n / np.linalg.norm(n)
        if not bg.clip_brush(brush, n, offset):
            continue
        convex = bg.get_convex(brush)
        newest = len(convex.planes) - 1
        assert any(f["plane"] == newest for f in convex.faces), (
            "cut %d (n=%s d=%s) was accepted but its plane contributes no face "
            "(%d planes, faces on %s)"
            % (step, tuple(np.round(n, 4)), offset, len(convex.planes),
               sorted({f["plane"] for f in convex.faces})))


def test_the_plane_set_grows_only_by_accepted_cuts():
    """Six box faces plus one plane per accepted cut, and not one more.

    Fio does not prune a plane whose face a later cut removed — that would
    renumber the planes, and face keys and component references are plane
    indices.  What must never happen is the set growing for any *other* reason:
    a duplicate plane, or a cut that was refused but appended anyway.
    """
    brush = box_brush("b", (0, 0, 0), (128, 128, 128))
    accepted = 0
    for normal, offset in REPEATED_CUTS:
        n = np.asarray(normal, dtype=float)
        if bg.clip_brush(brush, n / np.linalg.norm(n), offset):
            accepted += 1
    planes = brush["geometry"]["planes"]
    assert len(planes) == 6 + accepted, (
        "plane set is %d planes after %d accepted cuts, expected %d"
        % (len(planes), accepted, 6 + accepted))
    normals = [tuple(np.round(p["n"], 6)) for p in planes]
    assert len(set(normals)) == len(normals), \
        "the plane set contains duplicate normals: %s" % (normals,)


def test_clip_brush_syncs_pos_and_size_to_the_new_bounds():
    """Culling, the spatial grid and picking all read pos/size."""
    brush = box_brush("b", (0, 0, 0), (64, 64, 64))
    bg.clip_brush(brush, (0, 1, 0), 0.0)
    convex = bg.get_convex(brush)
    lo, hi = convex.bounds
    assert brush["pos"] == pytest.approx(list((lo + hi) * 0.5)), (
        "pos %s is not the centre of the clipped bounds %s..%s"
        % (brush["pos"], tuple(lo), tuple(hi)))
    assert brush["size"] == pytest.approx(list(hi - lo)), (
        "size %s is not the extent of the clipped bounds %s..%s"
        % (brush["size"], tuple(lo), tuple(hi)))


# ---------------------------------------------------------------------------
# Nearly-degenerate plane sets
# ---------------------------------------------------------------------------

def test_two_nearly_parallel_planes_still_make_a_solid():
    """A sliver brush is legal; it must not collapse into nothing."""
    planes = bg.box_planes((0, 0, 0), (256, 256, 256))
    n = np.array([0.0, 1.0, 1e-3])
    n /= np.linalg.norm(n)
    # Inside is dot(n, p) <= d for each, so the slab is -9 <= dot(n, p) <= 10:
    # 19 units thick, measured along n.
    planes = bg.clip_planes(planes, n, 10.0)
    planes = bg.clip_planes(planes, -n, 9.0)
    convex = bg.ConvexGeometry(planes)
    assert convex.is_valid, (
        "a thin slab between two near-parallel planes collapsed: %d verts"
        % len(convex.verts))
    heights = convex.verts @ n
    thickness = float(heights.max() - heights.min())
    assert thickness == pytest.approx(19.0, abs=1e-3), (
        "slab is %.4f units thick along n, expected the 19 the two planes bound "
        "(heights %.4f .. %.4f)" % (thickness, heights.min(), heights.max()))


def test_coincident_opposed_planes_produce_no_solid():
    """n·p <= 0 and -n·p <= 0 leave a plane, not a volume."""
    planes = bg.box_planes((0, 0, 0), (64, 64, 64))
    planes = bg.clip_planes(planes, (0, 1, 0), 0.0)
    planes = bg.clip_planes(planes, (0, -1, 0), 0.0)
    convex = bg.ConvexGeometry(planes)
    assert not convex.is_valid, (
        "two opposed coincident planes enclose no volume, but the geometry "
        "reported %d verts / %d faces" % (len(convex.verts), len(convex.faces)))


def test_an_empty_plane_set_is_not_a_solid():
    convex = bg.ConvexGeometry([])
    assert not convex.is_valid
    assert len(convex.verts) == 0
    assert convex.collision_triangles() == []
    assert convex.silhouette(0, 2) == []
    assert convex.bounds[0].tolist() == [0.0, 0.0, 0.0]


def test_a_single_plane_is_not_a_solid():
    convex = bg.ConvexGeometry([bg.make_plane((0, 1, 0), (0, 0, 0))])
    assert not convex.is_valid, "one half-space is unbounded, not a solid"


def test_extremely_acute_wedge_keeps_consistent_windings():
    """A 3-degree wedge is where welding tolerance is most likely to misbehave."""
    n = np.array([0.0, 1.0, math.tan(math.radians(3.0))])
    n /= np.linalg.norm(n)
    planes = bg.clip_planes(bg.box_planes((0, 0, 0), (1024, 64, 1024)), n, -20.0)
    convex = bg.ConvexGeometry(planes)
    assert convex.is_valid, "the wedge collapsed entirely"
    assert len(convex.verts) - len(convex.edges) + len(convex.faces) == 2, (
        "wedge is not a closed solid: V=%d E=%d F=%d"
        % (len(convex.verts), len(convex.edges), len(convex.faces)))


def test_clip_by_points_matches_a_clip_by_the_same_plane():
    p1, p2, p3 = (0.0, 0.0, 0.0), (0.0, 0.0, 10.0), (10.0, 10.0, 0.0)
    plane = bg.plane_from_points(p1, p2, p3)
    by_points = bg.ConvexGeometry(
        bg.clip_by_points(bg.box_planes((0, 0, 0), (64, 64, 64)), p1, p2, p3))
    by_plane = bg.ConvexGeometry(
        bg.clip_planes(bg.box_planes((0, 0, 0), (64, 64, 64)),
                       plane["n"], plane["d"]))
    assert np.allclose(by_points.verts, by_plane.verts), (
        "clip_by_points and clip_planes disagree for the same plane "
        "(n=%s d=%.6g)" % (tuple(np.round(plane["n"], 6)), plane["d"]))
