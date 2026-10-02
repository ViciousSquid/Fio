"""Regression tests for the convex-brush hardening pass (Fio 2.4).

Each test here pins a bug that was real, not a hypothetical: the plane set
growing on clips that cut nothing, coincident planes producing a doubled
surface, and derived geometry (whose faces carry texture and UV data, not just
shape) surviving an edit that should have invalidated it.

They also stress the plane set the way a mapper does — acute wedges, nearly
parallel cuts, very small brushes, long runs of component edits — since the
convex brush's plane set is Fio's only source of truth for brush geometry and
everything else is derived from it.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine import brush_geometry as bg  # noqa: E402


def make_box(pos=(0, 0, 0), size=(256, 256, 256), **extra):
    brush = {'id': 'test-brush', 'pos': list(pos), 'size': list(size)}
    brush.update(extra)
    return brush


# ---------------------------------------------------------------------------
# Clipping: a plane that bounds nothing must not join the set
# ---------------------------------------------------------------------------

def test_a_clip_that_misses_the_brush_changes_nothing():
    """Radiant frees an over-constraining face; Fio must not keep one.

    The plane set is rebuilt in O(planes^2), so a plane that bounds no surface
    is a permanent tax on every later rebuild — and it made the clip tool report
    that it had cut a brush it never touched.
    """
    brush = make_box()
    bg.box_to_geometry(brush)
    before = list(brush['geometry']['planes'])

    assert bg.clip_brush(brush, (1.0, 0.0, 0.0), 10_000.0) is False
    assert brush['geometry']['planes'] == before


def test_repeated_missed_clips_never_grow_the_plane_set():
    brush = make_box()
    bg.box_to_geometry(brush)
    for _ in range(20):
        bg.clip_brush(brush, (1.0, 0.0, 0.0), 10_000.0)
    assert len(brush['geometry']['planes']) == 6


def test_a_clip_on_a_plane_the_brush_already_has_is_refused():
    """Two coincident planes describe one half-space but make two windings."""
    brush = make_box()
    bg.box_to_geometry(brush)
    tris_before = len(bg.get_convex(brush).collision_triangles())

    # +X side of a 256 box centred on the origin is the plane x = 128.
    assert bg.clip_brush(brush, (1.0, 0.0, 0.0), 128.0) is False
    assert len(brush['geometry']['planes']) == 6
    assert len(bg.get_convex(brush).collision_triangles()) == tris_before


def test_a_clip_that_really_cuts_is_still_accepted():
    brush = make_box()
    assert bg.clip_brush(brush, (1.0, 1.0, 0.0), 0.0) is True
    assert len(brush['geometry']['planes']) == 7
    shape = bg.get_convex(brush)
    assert shape.is_valid
    # The cut plane is the last one, and it must actually bound a face.
    assert any(face['plane'] == 6 for face in shape.faces)


def test_a_clip_that_would_empty_the_brush_is_refused():
    brush = make_box()
    bg.box_to_geometry(brush)
    before = list(brush['geometry']['planes'])
    assert bg.clip_brush(brush, (1.0, 0.0, 0.0), -1000.0) is False
    assert brush['geometry']['planes'] == before


# ---------------------------------------------------------------------------
# Derived-geometry invalidation
# ---------------------------------------------------------------------------

def test_two_brushes_with_identical_geometry_have_distinct_signatures():
    """Renderer caches key on id(brush), which CPython reuses after a free.

    Without something per-object in the signature, a brush dict allocated at the
    address a deleted one used to hold inherits its cached GPU mesh.
    """
    a = make_box()
    b = make_box()
    bg.box_to_geometry(a)
    bg.box_to_geometry(b)
    assert bg.geometry_signature(a) != bg.geometry_signature(b)


def test_invalidating_a_brush_moves_its_signature():
    brush = make_box()
    bg.box_to_geometry(brush)
    before = bg.geometry_signature(brush)
    bg.invalidate_geometry_cache(brush)
    assert bg.geometry_signature(brush) != before


def test_a_box_brush_still_has_no_signature():
    """Signature is None for a brush with no plane set; callers rely on it."""
    assert bg.geometry_signature(make_box()) is None


def test_the_geometry_epoch_is_stripped_from_saved_brushes():
    """It is runtime bookkeeping, and must never reach a save or an undo blob."""
    assert '_geo_epoch' in bg.GEO_RUNTIME_KEYS


# ---------------------------------------------------------------------------
# Pathological convex geometry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("angle", [89.9, 89.99, 89.999, 89.9999, 0.001])
def test_nearly_parallel_cuts_stay_valid(angle):
    brush = make_box(size=(512, 512, 512))
    normal = (math.cos(math.radians(angle)), math.sin(math.radians(angle)), 0.0)
    assert bg.clip_brush(brush, normal, 0.0)
    shape = bg.get_convex(brush)
    assert shape.is_valid
    assert np.all(np.isfinite(shape.verts))


@pytest.mark.parametrize("size", [512.0, 64.0, 8.0, 1.0, 0.1, 0.01])
def test_small_brushes_keep_their_corners(size):
    brush = make_box(size=(size, size, size))
    shape = bg.get_shape(brush)
    assert shape.is_valid
    assert len(shape.verts) == 8
    assert len(shape.faces) == 6


def test_an_acute_wedge_survives_a_second_cut():
    brush = make_box(size=(512, 512, 512))
    assert bg.clip_brush(brush, (1.0, 0.05, 0.0), 0.0)
    assert bg.clip_brush(brush, (-1.0, 0.05, 0.0), 0.0)
    shape = bg.get_convex(brush)
    assert shape.is_valid
    assert np.all(np.isfinite(shape.verts))


def test_squashing_a_brush_flat_is_rejected_not_committed():
    """A drag that collapses a brush must leave the last valid shape alone."""
    brush = make_box()
    bg.box_to_geometry(brush)
    planes = brush['geometry']['planes']
    top = next(i for i, p in enumerate(planes) if p['n'][1] > 0.9)
    before = [dict(p) for p in planes]

    assert bg.offset_brush_planes(brush, {top: -128.0 + 1e-5}) is False
    assert brush['geometry']['planes'] == before
    assert bg.get_convex(brush).is_valid


def test_a_corner_flung_across_the_map_is_rejected():
    brush = make_box(size=(64, 64, 64))
    points = bg.brush_points(brush).copy()
    points[0] += np.array([bg.MAX_BRUSH_EXTENT * 2, 0.0, 0.0])
    assert bg.rebuild_brush_from_points(brush, points) is False


def test_repeated_component_edits_do_not_drift_or_accumulate_planes():
    """A vertex nudged out and back 200 times must land exactly where it began."""
    brush = make_box(size=(64, 64, 64))
    start = np.sort(bg.brush_points(brush).copy(), axis=0)
    out = np.array([1.0, 0.0, 0.0])

    for _ in range(200):
        points = bg.brush_points(brush).copy()
        points[0] += out
        assert bg.rebuild_brush_from_points(brush, points)
        points = bg.brush_points(brush).copy()
        moved = int(np.argmax(points @ np.array([1.0, -1.0, 1.0])))
        points[moved] -= out
        assert bg.rebuild_brush_from_points(brush, points)

    end = np.sort(bg.brush_points(brush), axis=0)
    assert end.shape == start.shape
    assert np.allclose(end, start, atol=1e-6)
    assert len(brush['geometry']['planes']) == 6


def test_a_heavily_clipped_brush_grows_only_by_cuts_that_bound_it():
    """The plane count tracks the cuts that landed, and nothing else.

    A later cut can make an *earlier* plane redundant (here the box's top is cut
    away entirely), and Fio keeps that plane rather than compacting the list.
    That is deliberate, not an oversight: a cut face is addressed as
    ``"#<plane index>"`` (``brush_geometry.face_key``), so renumbering the list
    would move every cut face's texture and UV transform onto a different
    surface — trading a bounded, self-limiting cost for a silent data bug.  What
    must not happen is unbounded growth, which is what the missed-clip guard
    above prevents.
    """
    brush = make_box(size=(512, 512, 512))
    cuts = ((1.0, 1.0, 0.0), (0.0, 1.0, 1.0), (1.0, 0.0, 1.0),
            (-1.0, 1.0, 0.0), (1.0, 1.0, 1.0))
    landed = sum(1 for normal in cuts if bg.clip_brush(brush, normal, 180.0))

    assert len(brush['geometry']['planes']) == 6 + landed
    shape = bg.get_convex(brush)
    assert shape.is_valid
    # Every *surface* face still points at a plane the brush actually has, and
    # no plane contributes twice.
    planes = [face['plane'] for face in shape.faces]
    assert len(planes) == len(set(planes))
    assert all(0 <= p < len(brush['geometry']['planes']) for p in planes)


def test_a_cut_face_keeps_its_key_when_a_later_cut_removes_another_plane():
    """The reason plane indices are never compacted, pinned as behaviour."""
    from editor import face_texture as ft

    brush = make_box(size=(512, 512, 512))
    assert bg.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    cut_key = next(k for k in ft.face_keys(brush) if k.startswith('#'))
    ft.set_transform(brush, cut_key, texture='CUT.png')

    # A second cut that removes the box's top entirely must not renumber it.
    assert bg.clip_brush(brush, (0.0, 1.0, 0.0), -100.0)
    assert cut_key in ft.face_keys(brush)
    assert ft.get_transform(brush, cut_key)['texture'] == 'CUT.png'


def test_bounds_stay_in_sync_with_the_plane_set_through_clips():
    """pos/size are a derived AABB; culling and the spatial grid read them."""
    brush = make_box(size=(256, 256, 256))
    assert bg.clip_brush(brush, (0.0, 1.0, 0.0), 0.0)     # lop off the top half
    shape = bg.get_convex(brush)
    lo, hi = shape.bounds
    assert np.allclose(brush['pos'], (lo + hi) * 0.5, atol=1e-6)
    assert np.allclose(brush['size'], hi - lo, atol=1e-6)
