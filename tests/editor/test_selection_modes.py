"""The selection primitives, at the level they are actually defined.

``editor.component_edit`` holds Radiant's four marquee modes and the drill-select
cycle as plain functions over object lists.  The 2D view suite drives them
through the main window; these test them directly, which is where the boundary
conditions are legible: what "touching" means at exactly the edge, whether a
tall selection really ignores depth, and which objects are eligible at all.

Qt-free: these functions take brush dicts and entities and return lists.
"""

import numpy as np
import pytest

from editor import component_edit as ce
from tests.helpers.worlds import box_brush

pytestmark = []


def _names(objects):
    return sorted(o.get("name") if isinstance(o, dict) else o.name
                  for o in objects)


@pytest.fixture
def scene():
    """Brushes at known places relative to a 100-unit region about the origin."""
    return {
        "inside": box_brush("inside", (0, 0, 0), (40, 40, 40)),
        "overlapping": box_brush("overlapping", (90, 0, 0), (60, 60, 60)),
        "outside": box_brush("outside", (500, 0, 0), (40, 40, 40)),
        "tall": box_brush("tall", (0, 4000, 0), (40, 40, 40)),
        "tall_wide": box_brush("tall_wide", (0, 4000, 0), (400, 40, 400)),
    }


REGION_LO = (-100.0, -100.0, -100.0)
REGION_HI = (100.0, 100.0, 100.0)


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------

def test_a_hidden_object_is_never_selectable():
    assert ce.is_selectable(box_brush("h", hidden=True)) is False
    assert ce.is_selectable(box_brush("h", hidden=True), skip_locked=False) is False, (
        "hidden is unconditional; only locked depends on the setting")


def test_a_locked_object_is_selectable_only_when_locks_are_ignored():
    locked = box_brush("l", lock=True)
    assert ce.is_selectable(locked) is False
    assert ce.is_selectable(locked, skip_locked=False) is True


def test_a_plain_object_is_selectable():
    assert ce.is_selectable(box_brush("plain")) is True


def test_eligibility_reads_an_entitys_properties_too():
    class _Entity:
        def __init__(self, **props):
            self.properties = props
            self.pos = [0, 0, 0]

    assert ce.is_hidden(_Entity(hidden=True)) is True
    assert ce.is_locked(_Entity(lock=True)) is True
    assert ce.is_selectable(_Entity()) is True


# ---------------------------------------------------------------------------
# Touching
# ---------------------------------------------------------------------------

def test_touching_takes_everything_that_overlaps_the_region(scene):
    picked = ce.select_touching(list(scene.values()), REGION_LO, REGION_HI)
    assert _names(picked) == ["inside", "overlapping"], (
        "touching selected %s; 'outside' is 500 units away and the tall "
        "brushes are 4000 units up" % (_names(picked),))


def test_touching_excludes_the_region_marker_itself(scene):
    marker = box_brush("marker", (0, 0, 0), (200, 200, 200))
    objects = list(scene.values()) + [marker]
    picked = ce.select_touching(objects, REGION_LO, REGION_HI, exclude=marker)
    assert "marker" not in _names(picked), (
        "the brush that defines the region selected itself")


def test_touching_skips_hidden_and_locked_objects(scene):
    scene["inside"]["hidden"] = True
    scene["overlapping"]["lock"] = True
    assert ce.select_touching(list(scene.values()), REGION_LO, REGION_HI) == []


def test_touching_includes_locked_objects_when_asked(scene):
    scene["overlapping"]["lock"] = True
    picked = ce.select_touching(list(scene.values()), REGION_LO, REGION_HI,
                                skip_locked=False)
    assert "overlapping" in _names(picked)


def test_touching_at_exactly_the_edge_counts(scene):
    """The edge case that decides whether a marquee feels right to use."""
    flush = box_brush("flush", (120, 0, 0), (40, 40, 40))   # spans x 100..140
    picked = ce.select_touching([flush], REGION_LO, REGION_HI)
    assert _names(picked) == ["flush"], (
        "a brush whose face is flush with the region boundary was not touched")


# ---------------------------------------------------------------------------
# Inside
# ---------------------------------------------------------------------------

def test_inside_takes_only_what_is_wholly_contained(scene):
    picked = ce.select_inside(list(scene.values()), REGION_LO, REGION_HI)
    assert _names(picked) == ["inside"], (
        "inside selected %s; 'overlapping' pokes out of the region"
        % (_names(picked),))


def test_inside_of_an_empty_region_is_empty():
    assert ce.select_inside([box_brush("a")], (0, 0, 0), (0, 0, 0)) == []


def test_inside_is_a_subset_of_touching(scene):
    objects = list(scene.values())
    inside = {id(o) for o in ce.select_inside(objects, REGION_LO, REGION_HI)}
    touching = {id(o) for o in ce.select_touching(objects, REGION_LO, REGION_HI)}
    assert inside <= touching, (
        "an object was 'inside' the region but not 'touching' it; the two "
        "modes disagree about the same geometry")


# ---------------------------------------------------------------------------
# Tall
# ---------------------------------------------------------------------------

def test_partial_tall_ignores_depth_along_the_views_axis(scene):
    """The marker's footprint is treated as infinitely tall."""
    picked = ce.select_partial_tall(list(scene.values()), REGION_LO, REGION_HI,
                                    axis1=0, axis2=2)
    assert _names(picked) == ["inside", "overlapping", "tall", "tall_wide"], (
        "partial tall selected %s; it should catch everything crossing the "
        "region's XZ column at any height" % (_names(picked),))


def test_complete_tall_requires_the_footprint_to_fit(scene):
    picked = ce.select_complete_tall(list(scene.values()), REGION_LO, REGION_HI,
                                     axis1=0, axis2=2)
    assert _names(picked) == ["inside", "tall"], (
        "complete tall selected %s; 'tall_wide' is 400 units across and does "
        "not fit the 200-unit column, and 'overlapping' pokes out"
        % (_names(picked),))


def test_complete_tall_is_a_subset_of_partial_tall(scene):
    objects = list(scene.values())
    complete = {id(o) for o in ce.select_complete_tall(
        objects, REGION_LO, REGION_HI, 0, 2)}
    partial = {id(o) for o in ce.select_partial_tall(
        objects, REGION_LO, REGION_HI, 0, 2)}
    assert complete <= partial


def test_the_tall_modes_respect_the_view_axes(scene):
    """Switching the 2D view changes which two axes form the column.

    From the top (x/z) the depth axis is Y, so a brush 4000 units up is caught
    and one 500 units along X is not.  From the side (y/z) the depth axis is X,
    so the opposite holds.
    """
    from_top = _names(ce.select_partial_tall(
        list(scene.values()), REGION_LO, REGION_HI, axis1=0, axis2=2))
    from_side = _names(ce.select_partial_tall(
        list(scene.values()), REGION_LO, REGION_HI, axis1=1, axis2=2))

    assert "tall" in from_top and "outside" not in from_top, (
        "from the top, depth is Y: the brush 4000 units up should be caught "
        "and the one 500 units along X should not; selected %s" % (from_top,))
    assert "outside" in from_side and "tall" not in from_side, (
        "from the side, depth is X: the brush 500 units along X should be "
        "caught and the one 4000 units up should not; selected %s"
        % (from_side,))


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

def test_object_bounds_of_a_brush_is_its_box():
    lo, hi = ce.object_bounds(box_brush("b", (10, 20, 30), (4, 6, 8)))
    assert list(lo) == pytest.approx([8, 17, 26])
    assert list(hi) == pytest.approx([12, 23, 34])


def test_object_bounds_of_an_entity_is_a_point():
    class _Entity:
        pos = [5.0, 6.0, 7.0]
        properties = {}

    lo, hi = ce.object_bounds(_Entity())
    assert list(lo) == pytest.approx([5, 6, 7])
    assert list(hi) == pytest.approx([5, 6, 7])


def test_object_bounds_of_an_angled_brush_uses_its_synced_box():
    from engine import brush_geometry as bg
    brush = box_brush("ramp", (0, 0, 0), (64, 64, 64))
    bg.clip_brush(brush, (0.0, 1.0, 0.0), 0.0)
    lo, hi = ce.object_bounds(brush)
    assert hi[1] == pytest.approx(0.0, abs=1e-3), (
        "the clipped brush's bounds still describe the unclipped box "
        "(top y=%.3f); marquee selection would use the wrong extent" % hi[1])


# ---------------------------------------------------------------------------
# Cycling (drill-select)
# ---------------------------------------------------------------------------

def test_cycling_with_nothing_selected_takes_the_first():
    a, b = box_brush("a"), box_brush("b")
    assert ce.cycle_pick([a, b], None) is a


def test_cycling_advances_through_the_candidates():
    a, b, c = box_brush("a"), box_brush("b"), box_brush("c")
    assert ce.cycle_pick([a, b, c], a) is b
    assert ce.cycle_pick([a, b, c], b) is c


def test_cycling_wraps_at_the_end():
    a, b = box_brush("a"), box_brush("b")
    assert ce.cycle_pick([a, b], b) is a, (
        "repeated clicks must walk the stack and come back round")


def test_cycling_with_a_selection_that_is_not_a_candidate_takes_the_first():
    a, b = box_brush("a"), box_brush("b")
    stranger = box_brush("stranger")
    assert ce.cycle_pick([a, b], stranger) is a


def test_cycling_with_no_candidates_selects_nothing():
    assert ce.cycle_pick([], None) is None
    assert ce.cycle_pick([], box_brush("a")) is None


def test_a_full_cycle_returns_to_where_it_started():
    brushes = [box_brush("a"), box_brush("b"), box_brush("c")]
    current = brushes[0]
    for _ in range(len(brushes)):
        current = ce.cycle_pick(brushes, current)
    assert current is brushes[0], (
        "cycling through %d candidates did not return to the first"
        % len(brushes))
