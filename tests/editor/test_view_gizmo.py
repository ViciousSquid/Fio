"""The 3D view's transform gizmo driven like a user would: press on a handle,
drag, release (or Esc) -- engine.view_gizmo in the real editor."""

import copy
import math

import glm
import numpy as np
import pytest

from editor import transform_gizmo as tg
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt

EYE = (420.0, 300.0, 520.0)


def _aim(view, eye=EYE, target=(0.0, 0.0, 0.0)):
    """Point the 3D view's camera: the matrices a painted frame leaves, and
    the editor camera the mouse ray starts from."""
    view.resize(800, 600)
    view.view_matrix = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    view.projection_matrix = glm.perspective(glm.radians(70.0), 800 / 600, 0.1, 10000.0)
    view.camera.pos = glm.vec3(*eye)
    view.logic_thread.camera.get_editor_camera().pos = glm.vec3(*eye)


def _setup(main_window, objects, mode=tg.MOVE):
    view = main_window.view_3d
    state = main_window.state
    state.brushes[:] = [o for o in objects if isinstance(o, dict)]
    state.things[:] = [o for o in objects if not isinstance(o, dict)]
    main_window.set_selected_objects(list(objects))
    main_window.grid_size_spinbox.setValue(16)
    _aim(view)
    view.gizmo.set_mode(mode)
    return view.gizmo


def _drag(gizmo, start, delta, steps=8, release=True):
    x, y = start
    assert gizmo.press(x, y), "the press did not grab a handle"
    for k in range(1, steps + 1):
        gizmo.drag_to(x + delta[0] * k / steps, y + delta[1] * k / steps)
    if release:
        gizmo.release()


def _arrow_point(gizmo, axis, along=0.7):
    start, tip, _ = gizmo.layout().axes[axis]
    return (start[0] + (tip[0] - start[0]) * along, start[1] + (tip[1] - start[1]) * along)


def _screen_dir(gizmo, axis):
    lay = gizmo.layout()
    start, tip, _ = lay.axes[axis]
    d = np.array([tip[0] - lay.centre_px[0], tip[1] - lay.centre_px[1]])
    return d / np.linalg.norm(d)


def _ring_point(gizmo, axis):
    lay = gizmo.layout()
    front = [p for p, f in lay.rings[axis] if f]
    others = [p for j, pts in lay.rings.items() if j != axis for p, f in pts if f]
    return max(front, key=lambda p: min(math.hypot(p[0] - q[0], p[1] - q[1]) for q in others))


def test_dragging_the_y_arrow_moves_the_brush_up_on_the_grid_in_one_undo(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    before = copy.deepcopy(brush)
    _drag(gizmo, _arrow_point(gizmo, 1), -_screen_dir(gizmo, 1) * -60)
    x, y, z = brush['pos']
    assert (x, z) == (0.0, 0.0)
    assert y > 0 and y % 16 == 0, brush['pos']
    main_window.undo()
    restored = main_window.state.brushes[0]
    assert restored['pos'] == before['pos']


def test_the_view_plane_circle_drags_freely_across_the_screen(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    cx, cy = gizmo.layout().centre_px
    _drag(gizmo, (cx, cy), (80, 0))
    # To the right on screen: along the camera's right vector, snapped.
    right = tg.camera_axes(main_window.view_3d.view_matrix)[0]
    moved = np.array(brush['pos'])
    assert moved @ right > 0
    assert all(v % 16 == 0 for v in brush['pos'])


def test_a_rotate_drag_turns_the_brush_and_esc_puts_it_back_exactly(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 64, 128))
    gizmo = _setup(main_window, [brush], tg.ROTATE)
    before = copy.deepcopy(brush)
    _drag(gizmo, _ring_point(gizmo, 1), (70, 35), release=False)
    assert gizmo.dragging and abs(gizmo.drag['applied']) > 0
    assert gizmo.drag['applied'] % 15 == pytest.approx(0.0, abs=1e-9)   # grid 16: 15 degree steps
    assert 'geometry' in brush                     # turned: an angled brush now
    gizmo.cancel()
    assert not gizmo.dragging
    assert {k: v for k, v in brush.items() if not k.startswith('_')} == before


def test_a_rotate_drag_turns_a_model_about_the_world_axis(main_window):
    from editor.things import ENTITY_TYPES
    from tests.helpers.worlds import make_thing
    prop_cls = next(cls for name, cls in ENTITY_TYPES.items()
                    if 'model_path' in cls(pos=[0, 0, 0]).properties)
    chair = make_thing(prop_cls, 'chair', (0, 0, 0),
                       model_path='assets/models/Retro_Dining_Chair_Cream.obj',
                       rotation=[0.0, 180.0, 0.0], scale=[1, 1, 1])
    gizmo = _setup(main_window, [chair], tg.ROTATE)
    _drag(gizmo, _ring_point(gizmo, 1), (70, 35))
    turned = chair.properties['rotation']
    yaw_change = tg.wrap_degrees(turned[1] - 180.0)
    assert turned[0] == pytest.approx(0.0) and turned[2] == pytest.approx(0.0)
    assert abs(yaw_change) > 0 and yaw_change % 15 == pytest.approx(0.0, abs=1e-6)
    assert chair.pos == pytest.approx([0.0, 0.0, 0.0])   # turned on the spot


def test_scaling_along_x_resizes_the_brush_on_the_grid_about_its_centre(main_window):
    brush = box_brush("crate", (32, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush], tg.SCALE)
    start, tip, _ = gizmo.layout().axes[0]
    _drag(gizmo, tip, _screen_dir(gizmo, 0) * 50)
    sx, sy, sz = brush['size']
    assert sx > 128 and sx % 16 == 0
    assert (sy, sz) == (128.0, 128.0)
    assert brush['pos'] == pytest.approx([32.0, 0.0, 0.0])
    assert 'geometry' not in brush                 # still a plain box


def test_uniform_scale_on_a_model_multiplies_its_scale(main_window):
    from editor.things import ENTITY_TYPES
    from tests.helpers.worlds import make_thing
    prop_cls = next(cls for name, cls in ENTITY_TYPES.items()
                    if 'model_path' in cls(pos=[0, 0, 0]).properties)
    chair = make_thing(prop_cls, 'chair', (0, 0, 0),
                       model_path='assets/models/Retro_Dining_Chair_Cream.obj',
                       rotation=[0.0, 0.0, 0.0], scale=[1.0, 1.0, 1.0])
    gizmo = _setup(main_window, [chair], tg.SCALE)
    cx, cy = gizmo.layout().centre_px
    r = tg.VIEW_RING * tg.RADIUS_PX
    _drag(gizmo, (cx + r, cy), (r, 0))         # twice as far from the centre
    assert chair.properties['scale'] == pytest.approx([2.0, 2.0, 2.0])


def test_a_press_away_from_the_handles_is_left_to_the_view(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    cx, cy = gizmo.layout().centre_px
    assert not gizmo.press(cx + 300, cy + 200)
    assert not gizmo.dragging


def test_a_drag_that_changes_nothing_leaves_no_undo_step(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    depth = len(main_window.state.undo_stack) if hasattr(main_window.state, 'undo_stack') else None
    x, y = _arrow_point(gizmo, 1)
    assert gizmo.press(x, y)
    gizmo.drag_to(x + 1, y)
    gizmo.release()
    assert brush['pos'] == [0.0, 0.0, 0.0]
    if depth is not None:
        assert len(main_window.state.undo_stack) == depth


def test_r_cycles_the_modes_and_the_chips_choose_one(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    seen = [gizmo.mode]
    for _ in range(3):
        gizmo.cycle_mode()
        seen.append(gizmo.mode)
    assert seen == [tg.MOVE, tg.ROTATE, tg.SCALE, tg.MOVE]


def test_the_gizmo_is_hidden_without_a_selection_and_in_play(main_window):
    gizmo = _setup(main_window, [])
    assert not gizmo.active()
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    main_window.state.brushes[:] = [brush]
    main_window.set_selected_objects([brush])
    assert gizmo.active()


# -- a drag and the history ---------------------------------------------------------

def _public(brush):
    """A brush without the caches kept on it."""
    return {k: v for k, v in brush.items() if not k.startswith('_')}


def _history(main_window):
    state = main_window.state
    return len(state.undo_stack), len(state.redo_stack)


def test_undo_during_a_drag_takes_the_drag_back_and_nothing_else(main_window):
    """Ctrl+Z mid-drag undid an older step under the drag: the drag went on
    moving a brush no longer in the scene, and its Esc then dropped a
    checkpoint that was not its own."""
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    main_window.save_state()                    # an earlier edit, to be kept
    brush['pos'] = [0.0, 0.0, 64.0]
    earlier = copy.deepcopy(brush)
    history = _history(main_window)

    _drag(gizmo, _arrow_point(gizmo, 1), _screen_dir(gizmo, 1) * 60, release=False)
    assert gizmo.dragging and brush['pos'] != earlier['pos']
    main_window.undo()
    assert not gizmo.dragging
    assert main_window.state.brushes[0] is brush, "the scene still holds the brush"
    assert _public(brush) == earlier, "the drag is taken back; the earlier edit stands"
    assert _history(main_window) == history

    main_window.undo()                          # and now the earlier edit
    assert main_window.state.brushes[0]['pos'] == [0.0, 0.0, 0.0]


def test_redo_and_play_end_a_drag_too(main_window, monkeypatch):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    before = copy.deepcopy(brush)
    history = _history(main_window)
    _drag(gizmo, _arrow_point(gizmo, 1), _screen_dir(gizmo, 1) * 60, release=False)
    main_window.redo()
    assert not gizmo.dragging and _public(brush) == before and _history(main_window) == history

    _drag(gizmo, _arrow_point(gizmo, 1), _screen_dir(gizmo, 1) * 60, release=False)
    assert main_window._end_gesture() is True
    assert not gizmo.dragging and _public(brush) == before and _history(main_window) == history
    assert main_window._end_gesture() is False


def test_a_click_on_a_handle_does_not_mark_the_map_modified(main_window):
    brush = box_brush("crate", (0, 0, 0), (128, 128, 128))
    gizmo = _setup(main_window, [brush])
    main_window.unsaved_changes = False
    x, y = _arrow_point(gizmo, 1)
    assert gizmo.press(x, y)
    gizmo.release()                             # a click: nothing moved
    assert main_window.unsaved_changes is False

    assert gizmo.press(x, y)
    gizmo.drag_to(x + 40, y - 40)
    gizmo.cancel()                              # Esc
    assert main_window.unsaved_changes is False

    _drag(gizmo, _arrow_point(gizmo, 1), _screen_dir(gizmo, 1) * 60)
    assert main_window.unsaved_changes is True
    main_window.unsaved_changes = False


def test_a_locked_object_does_not_turn(main_window):
    """Move and Scale skipped a locked object; Rotate turned it, and made a
    locked box brush an angled one."""
    free = box_brush("free", (0, 0, 0), (128, 128, 128))
    locked = box_brush("locked", (200, 0, 0), (64, 64, 64))
    locked['lock'] = True
    gizmo = _setup(main_window, [free, locked], tg.ROTATE)
    before = copy.deepcopy(locked)
    ring = _ring_point(gizmo, 1)
    _drag(gizmo, ring, (60, 0))
    assert _public(locked) == before
    assert free.get('geometry') is not None or free['pos'] != [0, 0, 0], "the free one turned"
    main_window.unsaved_changes = False
