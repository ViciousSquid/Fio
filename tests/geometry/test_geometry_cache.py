"""The derived-geometry cache: when it is reused, and when it must not be.

A brush's ``ConvexGeometry`` is expensive (the winding build is O(planes^2)) and
is cached on the brush dict itself under ``_geo_cache``.  Two things have to
hold for that to be safe:

* it must be *reused* while the brush is unchanged — otherwise every frame
  rebuilds every brush;
* it must be *dropped* the instant anything the derived surface depends on
  changes, including things the plane numbers do not show, like a texture swap.

The second is sharper than it looks because undo replaces brush dicts wholesale
and CPython readily hands a new dict the address a freed one had.  A cache keyed
on ``id(brush)`` would then serve a fresh brush the mesh of its dead predecessor.
The geometry *epoch* — a process-wide counter, never reused — is what closes
that, and it is what these tests pin down.
"""

import copy

import numpy as np
import pytest

from engine import brush_geometry as bg
from tests.helpers.worlds import box_brush


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------

def test_an_unchanged_brush_returns_the_same_geometry_object():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    first = bg.get_convex(brush)
    assert bg.get_convex(brush) is first, (
        "get_convex rebuilt the geometry for an unchanged brush - every "
        "consumer would pay the O(planes^2) winding build per call")


def test_a_box_brush_shape_cache_is_reused_without_promoting_it():
    """Component picking needs a shape for a plain box without angling it."""
    brush = box_brush("b")
    shape = bg.get_shape(brush)
    assert bg.get_shape(brush) is shape, "the box shape cache was not reused"
    assert "geometry" not in brush, (
        "get_shape must not promote a box brush to a plane set - that would "
        "lose the renderer's axis-aligned fast path")


def test_moving_a_box_brush_rebuilds_its_shape_cache():
    brush = box_brush("b")
    before = bg.get_shape(brush)
    brush["pos"] = [100.0, 0.0, 0.0]
    after = bg.get_shape(brush)
    assert after is not before, "the box shape cache survived a position change"
    assert after.center()[0] == pytest.approx(100.0), (
        "rebuilt box shape is centred at %s, expected x=100"
        % (tuple(np.round(after.center(), 4)),))


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

def test_clipping_replaces_the_cached_geometry():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    before = bg.get_convex(brush)
    bg.clip_brush(brush, (0, 1, 0), 0.0)
    after = bg.get_convex(brush)
    assert after is not before, "the cache survived a clip"
    assert after.bounds[1][1] == pytest.approx(0.0), (
        "cached geometry still describes the unclipped brush (top y=%.4f)"
        % after.bounds[1][1])


def test_translating_a_brush_replaces_the_cached_geometry():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    before = bg.get_convex(brush)
    bg.translate_brush(brush, (250.0, 0.0, 0.0))
    after = bg.get_convex(brush)
    assert after is not before, "the cache survived a translation"
    assert after.center()[0] == pytest.approx(250.0)


def test_a_texture_change_invalidates_the_cache_even_though_the_planes_match():
    """The Surface Inspector edits a face without touching ``n``/``d``.

    The winding carries the texture, so every consumer of the derived geometry
    should be showing something different - but the plane numbers are identical,
    which is exactly the case a naive plane-only signature would miss.
    """
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    before = bg.get_convex(brush)
    assert before.faces[0]["texture"] == "Dev/512.jpg"

    brush["geometry"]["planes"][0]["texture"] = "Dev/other.png"
    bg.invalidate_geometry_cache(brush)

    after = bg.get_convex(brush)
    assert after is not before, "the cache survived a texture edit"
    assert after.faces[0]["texture"] == "Dev/other.png", (
        "rebuilt geometry still carries the old texture %r"
        % after.faces[0]["texture"])


def test_invalidation_also_drops_the_box_shape_cache():
    brush = box_brush("b")
    bg.get_shape(brush)
    assert "_box_shape" in brush
    bg.invalidate_geometry_cache(brush)
    assert "_box_shape" not in brush, (
        "the box-derived picking shape outlived an invalidation; it is keyed "
        "off pos/size and is meaningless once the brush has a plane set")


# ---------------------------------------------------------------------------
# The epoch: the part that makes an id()-keyed cache honest
# ---------------------------------------------------------------------------

def test_two_brushes_never_share_a_geometry_epoch():
    a, b = box_brush("a"), box_brush("b")
    bg.box_to_geometry(a)
    bg.box_to_geometry(b)
    assert bg.geometry_signature(a)[0] != bg.geometry_signature(b)[0], (
        "two brushes were handed the same geometry epoch (%r) - a cache keyed "
        "on id(brush) could serve one the other's mesh"
        % (bg.geometry_signature(a)[0],))


def test_an_identical_copy_of_a_brush_gets_a_fresh_epoch():
    """This is the undo case: same planes, different dict, different identity."""
    original = box_brush("b")
    bg.box_to_geometry(original)
    original_sig = bg.geometry_signature(original)

    restored = copy.deepcopy({k: v for k, v in original.items()
                              if k not in bg.GEO_RUNTIME_KEYS})
    restored_sig = bg.geometry_signature(restored)

    assert restored_sig[1] == original_sig[1], (
        "the plane part of the signature should match - the planes are identical")
    assert restored_sig[0] != original_sig[0], (
        "the restored brush reused epoch %r; a renderer cache keyed on "
        "id(brush) would hand it the dead brush's GPU mesh"
        % (restored_sig[0],))
    assert restored_sig != original_sig


def test_every_invalidation_moves_the_epoch_forward():
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    seen = [bg.geometry_signature(brush)[0]]
    for _ in range(5):
        bg.invalidate_geometry_cache(brush)
        seen.append(bg.geometry_signature(brush)[0])
    assert seen == sorted(seen) and len(set(seen)) == len(seen), (
        "epochs must be strictly increasing and never repeat, got %s" % (seen,))


def test_a_sub_thousandth_plane_nudge_is_covered_by_the_epoch():
    """The plane part of the signature rounds; the epoch is what closes the gap."""
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    before = bg.geometry_signature(brush)

    brush["geometry"]["planes"][0]["d"] += 1e-6   # below the rounding
    assert bg.geometry_signature(brush)[1] == before[1], (
        "the rounded plane tuple was expected to be unchanged by a 1e-6 nudge")
    bg.invalidate_geometry_cache(brush)
    assert bg.geometry_signature(brush) != before, (
        "after an explicit invalidation the signature must differ even though "
        "the rounded planes match")


def test_geometry_signature_is_none_for_a_plain_box():
    assert bg.geometry_signature(box_brush("b")) is None, (
        "a brush with no plane set has no derived surface to sign")


# ---------------------------------------------------------------------------
# The cache must not be serialised
# ---------------------------------------------------------------------------

def test_every_runtime_key_the_module_writes_is_declared():
    """``editor_state`` strips ``GEO_RUNTIME_KEYS`` before saving and undoing.

    A key written here but missing from that set would be deep-copied into
    every undo checkpoint (a ConvexGeometry is not JSON-serialisable) or, worse,
    written into a saved map.
    """
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    bg.get_convex(brush)
    bg.get_shape(brush)
    bg.build_collision_mesh(brush)

    private = {k for k in brush if k.startswith("_")}
    undeclared = private - set(bg.GEO_RUNTIME_KEYS)
    assert not undeclared, (
        "brush_geometry wrote private key(s) %s that GEO_RUNTIME_KEYS does not "
        "declare; they would be serialised into saves and undo checkpoints"
        % sorted(undeclared))


def test_the_cached_geometry_owns_its_planes():
    """Mutating the brush's plane list must not corrupt a built geometry."""
    brush = box_brush("b")
    bg.box_to_geometry(brush)
    convex = bg.get_convex(brush)
    original_d = convex.planes[0]["d"]

    brush["geometry"]["planes"][0]["d"] = 9999.0

    assert convex.planes[0]["d"] == original_d, (
        "ConvexGeometry aliased the brush's plane dicts: editing the brush "
        "changed an already-built geometry from d=%s to d=%s"
        % (original_d, convex.planes[0]["d"]))
