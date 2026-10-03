"""Regression tests for component *identity* (Fio 2.4 hardening pass).

A component reference has to keep meaning the same piece of the same brush
across hovers, drags, geometry rebuilds and topology changes.  Two ways it used
not to:

* the overlay compared component *keys* without the brush, and a key is only
  unique within one brush — a face keys off its plane index, so ``('face', 0)``
  names the first plane of every brush in the scene;
* a reference whose component had been merged away was silently re-pointed at
  whatever corner happened to be nearest, at any distance, so the next drag
  moved geometry the user never picked.

These are Qt-free, like the model they test.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from editor import component_edit as ce  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


def make_box(pos=(0, 0, 0), size=(64, 64, 64), name='brush'):
    return {'id': name, 'name': name, 'pos': list(pos), 'size': list(size)}


# ---------------------------------------------------------------------------
# Highlighting must not bleed between brushes
# ---------------------------------------------------------------------------

def test_selecting_one_brushs_face_does_not_light_every_brushs_face():
    near, far = make_box(name='near'), make_box((1000, 0, 0), name='far')
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_FACE)
    controller.set_selection([ce.components(near, ce.MODE_FACE)[0]])

    overlay = controller.overlay([near, far])
    assert len(overlay['hot_points']) == 1
    assert np.allclose(overlay['hot_points'][0][0], 32.0)


def test_two_brushes_sharing_a_corner_highlight_separately():
    """Vertices key off position, so coincident corners share a key."""
    a, b = make_box(name='a'), make_box(name='b')      # exactly overlapping
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([ce.components(a, ce.MODE_VERTEX)[0]])

    assert len(controller.overlay([a, b])['hot_points']) == 1


def test_hovering_one_brushs_edge_does_not_light_anothers():
    a, b = make_box(name='a'), make_box(name='b')
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_EDGE)
    controller.set_hover(ce.components(a, ce.MODE_EDGE)[0])

    assert len(controller.overlay([a, b])['hot_lines']) == 1


def test_identity_distinguishes_brushes_but_equality_still_holds():
    a, b = make_box(name='a'), make_box(name='b')
    ref_a = ce.components(a, ce.MODE_FACE)[0]
    ref_b = ce.components(b, ce.MODE_FACE)[0]
    same_a = ce.components(a, ce.MODE_FACE)[0]

    assert ref_a.key == ref_b.key           # the key alone cannot tell them apart
    assert ref_a.identity != ref_b.identity
    assert ref_a != ref_b
    assert ref_a == same_a
    assert hash(ref_a) == hash(same_a)


# ---------------------------------------------------------------------------
# Re-resolving a reference after the geometry changed
# ---------------------------------------------------------------------------

def test_a_vertex_that_was_merged_away_resolves_to_nothing():
    brush = make_box()
    vanishing = ce.components(brush, ce.MODE_VERTEX)[0]

    points = bg.brush_points(brush)
    assert bg.rebuild_brush_from_points(brush, np.delete(points, 0, axis=0))

    assert ce.resolve_ref(brush, vanishing) is None


def test_a_dragged_vertex_keeps_its_selection_however_far_it_moved():
    """The drag knows its own delta, so the selection follows exactly."""
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    ref = ce.components(brush, ce.MODE_VERTEX)[0]
    controller.set_selection([ref])
    start = ref.position.copy()

    controller.begin_drag(ce.begin_component_drag([ref]))
    controller.update_drag(np.array([256.0, 0.0, 0.0]))
    controller.commit_drag()

    assert len(controller.selection) == 1
    moved = controller.selection[0].position
    assert np.allclose(moved, start + np.array([256.0, 0.0, 0.0]), atol=1e-6)


def test_an_unmoved_vertex_still_resolves_after_a_rebuild():
    brush = make_box()
    ref = ce.components(brush, ce.MODE_VERTEX)[0]
    assert bg.rebuild_brush_from_points(brush, bg.brush_points(brush).copy())
    resolved = ce.resolve_ref(brush, ref)
    assert resolved is not None
    assert np.allclose(resolved.position, ref.position)


def test_a_face_reference_resolves_by_plane_not_by_position():
    brush = make_box(size=(256, 256, 256))
    assert bg.clip_brush(brush, (1.0, 1.0, 0.0), 0.0)
    cut = next(r for r in ce.components(brush, ce.MODE_FACE) if r.plane == 6)

    # Slide the plane inward: its corners all move, its index does not.
    assert bg.offset_brush_planes(brush, {6: -40.0})
    resolved = ce.resolve_ref(brush, cut)
    assert resolved is not None
    assert resolved.plane == 6
    assert not np.allclose(resolved.position, cut.position)


# ---------------------------------------------------------------------------
# Pruning against a live scene
# ---------------------------------------------------------------------------

def test_pruning_drops_references_to_brushes_that_left_the_scene():
    kept, gone = make_box(name='kept'), make_box((500, 0, 0), name='gone')
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([ce.components(kept, ce.MODE_VERTEX)[0],
                              ce.components(gone, ce.MODE_VERTEX)[0]])

    controller.prune([kept])
    assert len(controller.selection) == 1
    assert controller.selection[0].brush is kept


def test_pruning_clears_a_hover_whose_brush_left_the_scene():
    kept, gone = make_box(name='kept'), make_box((500, 0, 0), name='gone')
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_hover(ce.components(gone, ce.MODE_VERTEX)[0])
    controller.prune([kept])
    assert controller.hover is None


# ---------------------------------------------------------------------------
# One press policy, shared by both viewports
# ---------------------------------------------------------------------------

def test_a_plain_press_selects_and_drags_the_component_under_the_cursor():
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    ref = ce.components(brush, ce.MODE_VERTEX)[2]

    drag = controller.press(ref)
    assert drag is not None
    assert controller.selection == [ref]
    assert controller.drag is drag


def test_a_shift_press_that_removes_a_component_starts_no_drag():
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    ref = ce.components(brush, ce.MODE_VERTEX)[0]
    controller.set_selection([ref])

    assert controller.press(ref, additive=True) is None
    assert controller.selection == []
    assert controller.drag is None


def test_a_shift_press_drags_the_whole_component_selection():
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    first, second = ce.components(brush, ce.MODE_VERTEX)[:2]
    controller.set_selection([first])

    drag = controller.press(second, additive=True)
    assert drag is not None
    assert len(controller.selection) == 2
    # Both corners move together.
    controller.update_drag(np.array([0.0, 16.0, 0.0]))
    assert controller.commit_drag()


def test_pressing_a_component_already_selected_keeps_the_rest_selected():
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    refs = ce.components(brush, ce.MODE_VERTEX)[:3]
    controller.set_selection(refs)

    controller.press(refs[1])
    assert len(controller.selection) == 3


def test_pressing_nothing_starts_no_drag():
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    assert controller.press(None) is None
    assert controller.drag is None


# ---------------------------------------------------------------------------
# Mode transitions
# ---------------------------------------------------------------------------

def test_leaving_component_mode_drops_the_component_selection():
    brush = make_box()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([ce.components(brush, ce.MODE_VERTEX)[0]])

    controller.set_mode(ce.MODE_OBJECT)
    assert controller.selection == []
    assert controller.hover is None
    assert not controller.is_component_mode()


def test_switching_component_mode_cancels_a_drag_in_flight():
    brush = make_box()
    before = bg.brush_points(brush).copy()
    controller = ce.ComponentController()
    controller.set_mode(ce.MODE_VERTEX)
    ref = ce.components(brush, ce.MODE_VERTEX)[0]
    controller.press(ref)
    controller.update_drag(np.array([32.0, 0.0, 0.0]))

    controller.set_mode(ce.MODE_EDGE)
    assert controller.drag is None
    assert np.allclose(np.sort(bg.brush_points(brush), axis=0),
                       np.sort(before, axis=0), atol=1e-6)
