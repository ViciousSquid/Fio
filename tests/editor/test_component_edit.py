"""Head-less tests for the unified component selection/drag model.

``editor.component_edit`` is deliberately Qt-free so the interaction rules it
encodes — what a click grabs, what a drag does to the geometry, what an area
selection catches — can be tested without a window or a GL context.
"""

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from editor import component_edit as ce  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


TOP_VIEW = (0, 2)       # the top view's axes: x and z, depth is y
DEPTH_AXIS = 1


def make_box(pos=(0, 0, 0), size=(64, 64, 64), **extra):
    brush = {
        'pos': list(pos),
        'size': list(size),
        'textures': {tag: 'tex_%s.png' % tag for tag in bg.FACE_TAGS},
    }
    brush.update(extra)
    return brush


def top_view_ray(u, v, depth=0.0):
    """Ray through a top-view cursor position, along the view's depth axis."""
    origin = np.zeros(3)
    origin[TOP_VIEW[0]] = u
    origin[TOP_VIEW[1]] = v
    origin[DEPTH_AXIS] = depth
    direction = np.zeros(3)
    direction[DEPTH_AXIS] = 1.0
    return origin, direction


def face_tags(brush, hits):
    shape = ce.brush_shape(brush)
    return [shape.planes[i].get('face') for i, _ in hits]


# ---------------------------------------------------------------------------
# Side selection — the rule behind direct side stretching
# ---------------------------------------------------------------------------

def test_side_select_grabs_the_side_just_outside_it():
    brush = make_box()
    origin, direction = top_view_ray(34, 0)
    hits = ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                          min_t=-math.inf)
    assert face_tags(brush, hits) == ['east']


def test_side_select_grabs_the_side_just_inside_it():
    """Pressing on the visible side works, not only beside it."""
    brush = make_box()
    origin, direction = top_view_ray(30, 0)
    hits = ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                          min_t=-math.inf)
    assert face_tags(brush, hits) == ['east']


def test_side_select_grabs_both_sides_at_a_corner():
    """A corner press takes two sides, so one drag resizes in two axes."""
    brush = make_box()
    origin, direction = top_view_ray(34, 34)
    hits = ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                          min_t=-math.inf)
    assert sorted(face_tags(brush, hits)) == ['east', 'north']


def test_side_select_ignores_the_middle_of_a_brush():
    """Pressing inside a brush still means 'move the brush', not 'stretch'."""
    brush = make_box()
    origin, direction = top_view_ray(0, 0)
    assert ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                          min_t=-math.inf) == []


def test_side_select_is_banded_so_empty_space_stays_free():
    """Far from the brush nothing is grabbed, leaving the marquee its drag."""
    brush = make_box()
    origin, direction = top_view_ray(200, 0)
    assert ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                          min_t=-math.inf) == []


def test_side_select_skips_sides_facing_the_screen():
    """In a top view the top/bottom faces are edge-on and not grabbable."""
    brush = make_box()
    for u, v in ((34, 0), (-34, 0), (0, 34), (0, -34)):
        origin, direction = top_view_ray(u, v)
        hits = ce.side_select(ce.brush_shape(brush), origin, direction, 6.0,
                              min_t=-math.inf)
        assert 'top' not in face_tags(brush, hits)
        assert 'down' not in face_tags(brush, hits)


def test_side_select_works_on_an_angled_brush():
    brush = make_box()
    assert bg.clip_brush(brush, [1.0, 0.0, 1.0], 0.0)     # diagonal cut in XZ
    shape = ce.brush_shape(brush)
    # Aim at the middle of the sloped cut, just outside it.
    cut = next(f for f in shape.faces if shape.planes[f['plane']].get('face') is None)
    centre = shape.verts[cut['indices']].mean(axis=0)
    outward = np.asarray(cut['normal']) * 2.0
    point = centre + outward
    origin, direction = top_view_ray(point[TOP_VIEW[0]], point[TOP_VIEW[1]],
                                     depth=point[DEPTH_AXIS])
    hits = ce.side_select(shape, origin, direction, 6.0, min_t=-math.inf)
    assert cut['plane'] in [i for i, _ in hits]


# ---------------------------------------------------------------------------
# Component enumeration and picking
# ---------------------------------------------------------------------------

def test_a_box_brush_exposes_components_without_becoming_geometry():
    brush = make_box()
    assert len(ce.components(brush, ce.MODE_VERTEX)) == 8
    assert len(ce.components(brush, ce.MODE_EDGE)) == 12
    assert len(ce.components(brush, ce.MODE_FACE)) == 6
    assert ce.components(brush, ce.MODE_OBJECT) == []
    assert 'geometry' not in brush


def test_pick_vertex_in_2d_finds_the_nearest_corner():
    brush = make_box()
    ref = ce.pick_component_2d([brush], ce.MODE_VERTEX, (32, 32),
                               TOP_VIEW[0], TOP_VIEW[1], 8.0)
    assert ref is not None
    assert ref.kind == ce.MODE_VERTEX
    assert abs(ref.position[0] - 32) < 1e-6
    assert abs(ref.position[2] - 32) < 1e-6


def test_pick_vertex_in_2d_respects_the_tolerance():
    brush = make_box()
    assert ce.pick_component_2d([brush], ce.MODE_VERTEX, (0, 0),
                                TOP_VIEW[0], TOP_VIEW[1], 8.0) is None


def test_pick_edge_in_2d_finds_an_edge_between_corners():
    brush = make_box()
    ref = ce.pick_component_2d([brush], ce.MODE_EDGE, (32, 0),
                               TOP_VIEW[0], TOP_VIEW[1], 8.0)
    assert ref is not None and ref.kind == ce.MODE_EDGE
    assert len(ref.indices) == 2


def test_pick_face_in_2d_uses_the_side_rule():
    brush = make_box()
    ref = ce.pick_component_2d([brush], ce.MODE_FACE, (33, 0),
                               TOP_VIEW[0], TOP_VIEW[1], 6.0)
    assert ref is not None and ref.kind == ce.MODE_FACE
    assert ce.brush_shape(brush).planes[ref.plane].get('face') == 'east'


def test_picking_only_tests_the_brushes_it_is_given():
    """Component picking is selection-scoped; an unlisted brush is invisible."""
    selected = make_box(pos=(0, 0, 0))
    other = make_box(pos=(0, 0, 0), size=(8, 8, 8))
    ref = ce.pick_component_2d([selected], ce.MODE_VERTEX, (4, 4),
                               TOP_VIEW[0], TOP_VIEW[1], 8.0)
    assert ref is None or ref.brush is selected
    assert ce.brush_shape(other) is not None      # the other brush is fine


def test_pick_vertex_in_3d():
    brush = make_box()
    origin = np.array([200.0, 200.0, 200.0])
    direction = (np.array([32.0, 32.0, 32.0]) - origin)
    direction /= np.linalg.norm(direction)
    ref = ce.pick_component_3d([brush], ce.MODE_VERTEX, origin, direction, 0.02)
    assert ref is not None
    assert np.allclose(ref.position, [32, 32, 32])


def test_pick_face_in_3d_hits_the_face_the_ray_enters():
    brush = make_box()
    ref = ce.pick_component_3d([brush], ce.MODE_FACE,
                               [300.0, 0.0, 0.0], [-1.0, 0.0, 0.0], 0.02)
    assert ref is not None
    assert ce.brush_shape(brush).planes[ref.plane].get('face') == 'east'


def test_pick_returns_nothing_in_object_mode():
    brush = make_box()
    assert ce.pick_component_2d([brush], ce.MODE_OBJECT, (32, 32),
                                TOP_VIEW[0], TOP_VIEW[1], 8.0) is None
    assert ce.pick_component_3d([brush], ce.MODE_OBJECT,
                                [300.0, 0.0, 0.0], [-1.0, 0.0, 0.0], 0.02) is None


# ---------------------------------------------------------------------------
# Drags
# ---------------------------------------------------------------------------

def vertex_ref(brush, position):
    return next(r for r in ce.components(brush, ce.MODE_VERTEX)
                if np.allclose(r.position, position))


def face_ref(brush, tag):
    shape = ce.brush_shape(brush)
    return next(r for r in ce.components(brush, ce.MODE_FACE)
                if shape.planes[r.plane].get('face') == tag)


def test_side_stretch_resizes_the_brush():
    brush = make_box()
    origin, direction = top_view_ray(34, 0)
    drag, picked = ce.begin_side_stretch([brush], origin, direction, 6.0,
                                         min_t=-math.inf)
    assert drag is not None and len(picked) == 1
    assert drag.update([16.0, 0.0, 0.0])
    assert drag.commit()
    assert brush['size'][0] == pytest.approx(80.0)
    assert brush['pos'][0] == pytest.approx(8.0)


def test_side_stretch_leaves_a_box_a_box():
    """A stretched box stays in the compact pos/size form, not a plane set."""
    brush = make_box()
    origin, direction = top_view_ray(34, 0)
    drag, _ = ce.begin_side_stretch([brush], origin, direction, 6.0,
                                    min_t=-math.inf)
    drag.update([16.0, 0.0, 0.0])
    drag.commit()
    assert 'geometry' not in brush


def test_side_stretch_at_a_corner_moves_two_axes():
    brush = make_box()
    origin, direction = top_view_ray(34, 34)
    drag, picked = ce.begin_side_stretch([brush], origin, direction, 6.0,
                                         min_t=-math.inf)
    assert len(picked) == 2
    drag.update([16.0, 0.0, 16.0])
    drag.commit()
    assert brush['size'][0] == pytest.approx(80.0)
    assert brush['size'][2] == pytest.approx(80.0)
    assert brush['size'][1] == pytest.approx(64.0)


def test_side_stretch_spans_a_multi_selection():
    a = make_box(pos=(0, 0, 0))
    b = make_box(pos=(0, 0, 200))
    origin, direction = top_view_ray(34, 0)
    # Only `a` has a side under the cursor, so only `a` is grabbed.
    drag, picked = ce.begin_side_stretch([a, b], origin, direction, 6.0,
                                         min_t=-math.inf)
    assert {id(brush) for brush, _ in picked} == {id(a)}
    drag.update([16.0, 0.0, 0.0])
    drag.commit()
    assert a['size'][0] == pytest.approx(80.0)
    assert b['size'][0] == pytest.approx(64.0)


def test_vertex_drag_moves_one_corner():
    brush = make_box()
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    assert drag.update([0.0, 16.0, 0.0])
    assert drag.commit()
    assert brush['size'][1] == pytest.approx(80.0)
    points = ce.brush_points(brush)
    assert any(np.allclose(p, [32, 48, 32], atol=1e-6) for p in points)


def test_vertex_drag_keeps_the_brush_convex():
    brush = make_box()
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    drag.update([90.0, 40.0, -70.0])
    drag.commit()
    convex = bg.get_convex(brush)
    normals, offsets = convex.plane_arrays()
    assert np.all(convex.verts @ normals.T - offsets <= 1e-3)


def test_vertex_drag_rejects_a_collapse_and_keeps_the_last_good_shape():
    brush = make_box()
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    drag.update([0.0, 16.0, 0.0])           # a valid step
    good = ce.brush_points(brush).copy()
    drag.update([0.0, -100000.0, 0.0])      # a corner flung across the map
    assert drag.rejected
    assert np.allclose(np.sort(ce.brush_points(brush), axis=0),
                       np.sort(good, axis=0))


def test_a_drag_recomputes_from_the_press_snapshot_not_incrementally():
    """Repeated updates must not accumulate: the last delta is the whole move."""
    brush = make_box()
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    for step in (4.0, 8.0, 16.0, 32.0, 16.0):
        drag.update([0.0, step, 0.0])
    drag.commit()
    assert brush['size'][1] == pytest.approx(80.0)   # 64 + the final 16


def test_edge_drag_moves_both_of_its_corners():
    brush = make_box()
    ref = next(r for r in ce.components(brush, ce.MODE_EDGE)
               if np.allclose(r.position, [32, 32, 0]))
    before = set(map(tuple, np.round(ce.brush_points(brush), 4)))
    drag = ce.begin_component_drag([ref])
    assert drag.update([16.0, 0.0, 0.0])
    assert drag.commit()
    after = set(map(tuple, np.round(ce.brush_points(brush), 4)))
    moved = after - before
    assert len(moved) == 2
    assert all(abs(p[0] - 48.0) < 1e-3 for p in moved)


def test_face_drag_moves_the_supporting_plane():
    brush = make_box()
    drag = ce.begin_component_drag([face_ref(brush, 'east')])
    assert drag.update([32.0, 0.0, 0.0])
    assert drag.commit()
    assert brush['size'][0] == pytest.approx(96.0)
    assert brush['size'][1] == pytest.approx(64.0)


def test_face_drag_leaves_the_neighbouring_faces_alone():
    brush = make_box()
    drag = ce.begin_component_drag([face_ref(brush, 'east')])
    drag.update([32.0, 0.0, 0.0])
    drag.commit()
    shape = ce.brush_shape(brush)
    west = next(f for f in shape.faces
                if shape.planes[f['plane']].get('face') == 'west')
    assert all(abs(shape.verts[i][0] + 32.0) < 1e-6 for i in west['indices'])


def test_face_shear_drag_tilts_the_neighbours_instead():
    brush = make_box()
    drag = ce.begin_component_drag([face_ref(brush, 'east')], shear=True)
    assert drag.update([0.0, 32.0, 0.0])
    assert drag.commit()
    # The east corners have slid up; the west ones have not, so the top and
    # bottom faces are now sloped rather than axis-aligned.
    assert bg.brush_has_geometry(brush)
    assert not bg.is_axis_aligned_box(brush)
    points = ce.brush_points(brush)
    east_points = points[np.abs(points[:, 0] - 32.0) < 1e-6]
    assert np.all(east_points[:, 1] > -32.0 + 1e-6)


def test_cancelling_a_drag_restores_the_brush_exactly():
    brush = make_box()
    before_pos = list(brush['pos'])
    before_size = list(brush['size'])
    drag = ce.begin_component_drag([vertex_ref(brush, [32, 32, 32])])
    drag.update([0.0, 48.0, 0.0])
    drag.cancel()
    assert brush['pos'] == before_pos
    assert brush['size'] == before_size
    assert 'geometry' not in brush


def test_cancelling_a_side_stretch_restores_a_box_brush():
    brush = make_box()
    origin, direction = top_view_ray(34, 0)
    drag, _ = ce.begin_side_stretch([brush], origin, direction, 6.0,
                                    min_t=-math.inf)
    drag.update([64.0, 0.0, 0.0])
    drag.cancel()
    assert 'geometry' not in brush
    assert brush['size'] == [64, 64, 64]


def test_drag_of_an_angled_brush_preserves_its_slope_family():
    brush = make_box()
    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)
    shape = ce.brush_shape(brush)
    cut = next(f for f in shape.faces if shape.planes[f['plane']].get('face') is None)
    ref = ce.ComponentRef(brush, ce.MODE_FACE, cut['indices'],
                          plane=cut['plane'],
                          position=shape.verts[cut['indices']].mean(axis=0))
    drag = ce.begin_component_drag([ref])
    assert drag.update([8.0, 8.0, 0.0])
    assert drag.commit()
    convex = bg.get_convex(brush)
    assert convex.is_valid
    # The cut plane kept its orientation and only slid along its own normal.
    moved = convex.planes[cut['plane']]
    assert moved['n'] == pytest.approx([math.sqrt(0.5), math.sqrt(0.5), 0.0])


# ---------------------------------------------------------------------------
# Snapping
# ---------------------------------------------------------------------------

def test_snap_delta_rounds_to_the_grid():
    assert ce.snap_delta([17.0, 3.0, -9.0], 16) == pytest.approx([16.0, 0.0, -16.0])


def test_snap_delta_can_be_limited_to_the_view_axes():
    out = ce.snap_delta([17.0, 3.0, -9.0], 16, axes=(0, 2))
    assert out == pytest.approx([16.0, 3.0, -16.0])


def test_snap_delta_off_when_the_grid_is_disabled():
    assert ce.snap_delta([17.0, 3.0, -9.0], 0) == pytest.approx([17.0, 3.0, -9.0])


def test_component_snap_lands_the_component_on_the_grid():
    # A corner at 30 dragged by 5 should land on 32, not on 30 + 0.
    delta = ce.snap_component_delta([30.0, 0.0, 0.0], [5.0, 0.0, 0.0], 16)
    assert (30.0 + delta[0]) == pytest.approx(32.0)


def test_component_snap_is_a_no_op_without_a_grid():
    delta = ce.snap_component_delta([30.0, 0.0, 0.0], [5.0, 0.0, 0.0], 0)
    assert delta == pytest.approx([5.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# Selection cycling and area selections
# ---------------------------------------------------------------------------

def test_cycle_walks_the_candidates_and_wraps():
    a, b, c = object(), object(), object()
    assert ce.cycle_pick([a, b, c], None) is a
    assert ce.cycle_pick([a, b, c], a) is b
    assert ce.cycle_pick([a, b, c], c) is a


def test_cycle_starts_over_when_the_current_pick_is_elsewhere():
    a, b = object(), object()
    assert ce.cycle_pick([a, b], object()) is a
    assert ce.cycle_pick([], a) is None


def test_cycle_is_deterministic_over_repeated_passes():
    items = [object() for _ in range(4)]
    current = None
    visited = []
    for _ in range(8):
        current = ce.cycle_pick(items, current)
        visited.append(items.index(current))
    assert visited == [0, 1, 2, 3, 0, 1, 2, 3]


def area_scene():
    region = make_box(pos=(0, 0, 0), size=(128, 128, 128))
    return {
        'region': region,
        'inside': make_box(pos=(0, 0, 0), size=(32, 32, 32)),
        'overlapping': make_box(pos=(80, 0, 0), size=(64, 64, 64)),
        'above': make_box(pos=(0, 400, 0), size=(32, 32, 32)),
        'elsewhere': make_box(pos=(900, 0, 0), size=(32, 32, 32)),
        'hidden': make_box(pos=(0, 0, 0), size=(16, 16, 16), hidden=True),
        'locked': make_box(pos=(0, 0, 0), size=(16, 16, 16), lock=True),
    }


def names(scene, hits):
    lookup = {id(v): k for k, v in scene.items()}
    return sorted(lookup[id(h)] for h in hits)


def test_select_touching_catches_overlap_but_not_distance():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    hits = ce.select_touching(list(scene.values()), lo, hi,
                              exclude=scene['region'], skip_locked=False)
    assert names(scene, hits) == ['inside', 'locked', 'overlapping']


def test_select_inside_needs_full_containment():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    hits = ce.select_inside(list(scene.values()), lo, hi,
                            exclude=scene['region'], skip_locked=False)
    assert names(scene, hits) == ['inside', 'locked']


def test_select_partial_tall_reaches_up_the_column():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    hits = ce.select_partial_tall(list(scene.values()), lo, hi, 0, 2,
                                  exclude=scene['region'], skip_locked=False)
    assert names(scene, hits) == ['above', 'inside', 'locked', 'overlapping']


def test_select_complete_tall_needs_the_footprint_inside():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    hits = ce.select_complete_tall(list(scene.values()), lo, hi, 0, 2,
                                   exclude=scene['region'], skip_locked=False)
    assert names(scene, hits) == ['above', 'inside', 'locked']


def test_area_selections_skip_hidden_objects_always():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    for op in (ce.select_touching, ce.select_inside):
        hits = op(list(scene.values()), lo, hi, exclude=scene['region'])
        assert scene['hidden'] not in hits


def test_area_selections_skip_locked_objects_by_default():
    scene = area_scene()
    lo, hi = ce.object_bounds(scene['region'])
    hits = ce.select_inside(list(scene.values()), lo, hi,
                            exclude=scene['region'])
    assert names(scene, hits) == ['inside']


def test_a_shear_that_flattens_a_brush_is_rejected():
    """Shearing a face onto its opposite would leave no volume: refuse it."""
    brush = make_box()
    drag = ce.begin_component_drag([face_ref(brush, 'east')], shear=True)
    assert drag.update([-64.0, 0.0, 0.0]) is False
    assert drag.rejected
    assert brush['size'] == [64, 64, 64]


def test_object_bounds_of_an_angled_brush_follows_its_geometry():
    brush = make_box()
    assert bg.clip_brush(brush, [0.0, 1.0, 0.0], 0.0)   # lop off the top half
    lo, hi = ce.object_bounds(brush)
    assert hi[1] == pytest.approx(0.0)
    assert lo[1] == pytest.approx(-32.0)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

def test_mode_switching_clears_hover_and_selection():
    controller = ce.ComponentController()
    brush = make_box()
    assert controller.mode == ce.MODE_OBJECT
    assert controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([vertex_ref(brush, [32, 32, 32])])
    controller.set_hover(vertex_ref(brush, [-32, 32, 32]))
    assert controller.set_mode(ce.MODE_EDGE)
    assert controller.selection == []
    assert controller.hover is None
    assert not controller.set_mode(ce.MODE_EDGE)     # already there
    assert not controller.set_mode('nonsense')


def test_is_component_mode():
    controller = ce.ComponentController()
    assert not controller.is_component_mode()
    controller.set_mode(ce.MODE_FACE)
    assert controller.is_component_mode()


def test_toggle_adds_then_removes_a_component():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    ref = vertex_ref(brush, [32, 32, 32])
    controller.toggle(ref)
    assert len(controller.selection) == 1
    controller.toggle(vertex_ref(brush, [32, 32, 32]))   # same component again
    assert controller.selection == []


def test_hover_only_bumps_the_version_when_it_changes():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    ref = vertex_ref(brush, [32, 32, 32])
    assert controller.set_hover(ref)
    version = controller.version
    assert not controller.set_hover(vertex_ref(brush, [32, 32, 32]))
    assert controller.version == version


def test_overlay_is_cached_until_the_version_moves():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    first = controller.overlay([brush])
    assert controller.overlay([brush]) is first      # no rebuild
    assert first['points'].shape == (8, 3)
    assert first['points'].dtype == np.float32
    controller.invalidate()
    assert controller.overlay([brush]) is not first


def test_overlay_marks_the_hovered_component_hot():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_hover(vertex_ref(brush, [32, 32, 32]))
    data = controller.overlay([brush])
    assert data['hot_points'].shape == (1, 3)
    assert data['points'].shape == (7, 3)


def test_overlay_is_empty_in_object_mode():
    controller = ce.ComponentController()
    brush = make_box()
    data = controller.overlay([brush])
    assert all(len(v) == 0 for v in data.values())


def test_overlay_draws_edges_in_edge_mode():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_EDGE)
    data = controller.overlay([brush])
    assert data['lines'].shape == (12, 2, 3)
    assert data['points'].shape == (0, 3)


def test_controller_drag_lifecycle_updates_and_commits_once():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    ref = vertex_ref(brush, [32, 32, 32])
    controller.set_selection([ref])
    drag = ce.begin_component_drag([ref])
    assert controller.begin_drag(drag)
    assert controller.update_drag([0.0, 16.0, 0.0])
    assert controller.commit_drag()
    assert controller.drag is None
    assert brush['size'][1] == pytest.approx(80.0)


def test_controller_cancel_drag_restores_and_clears():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    controller.begin_drag(drag)
    controller.update_drag([0.0, 48.0, 0.0])
    assert controller.cancel_drag()
    assert controller.drag is None
    assert brush['size'] == [64, 64, 64]
    assert not controller.cancel_drag()      # nothing left to cancel


def test_prune_drops_references_to_brushes_that_are_gone():
    controller = ce.ComponentController()
    kept = make_box(pos=(0, 0, 0))
    removed = make_box(pos=(200, 0, 0))
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_selection([vertex_ref(kept, [32, 32, 32]),
                              vertex_ref(removed, [232, 32, 32])])
    controller.prune([kept])
    assert len(controller.selection) == 1
    assert controller.selection[0].brush is kept


def test_prune_clears_a_stale_hover():
    controller = ce.ComponentController()
    brush = make_box()
    controller.set_mode(ce.MODE_VERTEX)
    controller.set_hover(vertex_ref(brush, [32, 32, 32]))
    controller.prune([])
    assert controller.hover is None


def test_resolve_ref_follows_a_component_through_an_edit():
    brush = make_box()
    ref = vertex_ref(brush, [32, 32, 32])
    drag = ce.begin_component_drag([ref])
    drag.update([0.0, 16.0, 0.0])
    drag.commit()
    resolved = ce.resolve_ref(brush, ref)
    assert resolved is not None
    assert resolved.kind == ce.MODE_VERTEX


def test_component_refs_compare_by_brush_and_position():
    brush = make_box()
    other = make_box()
    a = vertex_ref(brush, [32, 32, 32])
    b = vertex_ref(brush, [32, 32, 32])
    c = vertex_ref(other, [32, 32, 32])
    assert a == b and hash(a) == hash(b)
    assert a != c
    assert a != 'not a ref'


def test_is_selectable_respects_hidden_and_locked():
    assert ce.is_selectable(make_box())
    assert not ce.is_selectable(make_box(hidden=True))
    assert not ce.is_selectable(make_box(lock=True))
    assert ce.is_selectable(make_box(lock=True), skip_locked=False)
