"""Interaction tests for component editing in a 2D view.

These drive the real ``View2D`` mouse handlers and the real ``MainWindow``
selection logic against a lightweight host that stands in for the widgets a
full editor window would own.  The 3D viewport is the only part replaced by a
stub: it needs a GL context, which a test runner has no business creating.

What is under test here is the interaction contract rather than the geometry:
which gesture starts which drag, and that one continuous drag produces exactly
one undo step.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

import configparser  # noqa: E402

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from editor import component_edit as ce  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402
from editor.main_window import MainWindow  # noqa: E402
from editor.view_2d import View2D  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


class _Stub3DView:
    """Stands in for the GL viewport, which cannot exist in a test runner."""

    play_mode = False
    selected_object = None
    face_mode_active = False
    grid_size = 16

    def update(self):
        pass


class _StubTabs:
    """Stands in for the 2D-view tab widget the clone offset is read from."""

    def __init__(self, view):
        self._view = view

    def currentWidget(self):
        return self._view


class _StubSpin:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _StubPropertyEditor:
    def __init__(self):
        self.target = None

    def set_object(self, obj):
        self.target = obj


class FakeEditorWindow(QWidget):
    """A host with just enough of MainWindow for the 2D view to talk to.

    The selection and component logic are the *real* MainWindow methods, bound
    onto this object, so the tests exercise shipping code rather than a
    reimplementation of it.
    """

    # Real implementations, bound to this stand-in host.
    _selected_brushes = MainWindow._selected_brushes
    component_drag_targets = MainWindow.component_drag_targets
    _selection_skips_locked = MainWindow._selection_skips_locked
    _marker_brush = MainWindow._marker_brush
    _area_selection_objects = MainWindow._area_selection_objects
    _apply_area_selection = MainWindow._apply_area_selection
    select_touching = MainWindow.select_touching
    select_inside = MainWindow.select_inside
    select_partial_tall = MainWindow.select_partial_tall
    select_complete_tall = MainWindow.select_complete_tall
    set_component_mode = MainWindow.set_component_mode
    cycle_component_mode = MainWindow.cycle_component_mode
    _sync_component_buttons = MainWindow._sync_component_buttons
    clone_placement_active = MainWindow.clone_placement_active
    move_clone_placement = MainWindow.move_clone_placement
    finish_clone_placement = MainWindow.finish_clone_placement
    cancel_clone_placement = MainWindow.cancel_clone_placement
    _translate_object = staticmethod(MainWindow._translate_object)
    _copy_name = staticmethod(MainWindow._copy_name)
    selection_centre = MainWindow.selection_centre
    selected_objects_list = MainWindow.selected_objects_list
    apply_rotation_to_selection = MainWindow.apply_rotation_to_selection
    apply_clip_to_selection = MainWindow.apply_clip_to_selection

    def __init__(self):
        super().__init__()
        self.config = configparser.ConfigParser()
        self.config.add_section('Controls')
        self.config.add_section('Display')
        self.state = EditorState()
        self.state.selected_objects = []
        self.components = ce.ComponentController()
        self.clone_placement = None
        self.tool_mode = 'select'
        self.clip_mode = False
        self.rotate_mode = False
        self.grid_visible = True
        self.unsaved_changes = False
        self.view_3d = _Stub3DView()
        self.property_editor = _StubPropertyEditor()
        self.toasts = []

    # -- the widget-bound bits the real window would provide ---------------
    def save_state(self):
        self.state.save_state()
        self.components.invalidate()

    def show_toast(self, message, is_error=False, duration=None):
        self.toasts.append(message)

    def refresh_component_views(self):
        pass

    def update_all_ui(self):
        self.property_editor.set_object(self.state.selected_object)

    def update_views(self):
        pass

    def set_selected_object(self, obj):
        self.components.clear()
        self.components.invalidate()
        self.state.selected_objects = [] if obj is None else [obj]
        self.state.selected_object = obj
        self.update_all_ui()

    def set_selected_objects(self, objects):
        self.components.clear()
        self.components.invalidate()
        self.state.selected_objects = list(objects or [])
        self.state.selected_object = objects[0] if objects else None
        self.update_all_ui()

    def _active_2d_view(self):
        return self.view_top


def make_box(pos=(0, 0, 0), size=(64, 64, 64), **extra):
    brush = {
        'pos': list(pos),
        'size': list(size),
        'textures': {tag: 'tex_%s.png' % tag for tag in bg.FACE_TAGS},
    }
    brush.update(extra)
    return brush


@pytest.fixture
def editor(qt_app):
    host = FakeEditorWindow()
    view = View2D(host, host, 'top')
    view.resize(800, 600)
    view.zoom_factor = 1.0
    view.pan_offset = QPointF(0.0, 0.0)
    view.grid_size = 16
    view.snap_to_grid_enabled = True
    host.view_top = view
    host.view_side = view
    host.view_front = view
    return host, view


def press(view, world_point, modifiers=Qt.NoModifier, button=Qt.LeftButton):
    screen = view.world_to_screen(QPointF(*world_point)).toPoint()
    view.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, screen, button,
                                     button, modifiers))
    return screen


def move(view, world_point, buttons=Qt.LeftButton, modifiers=Qt.NoModifier):
    screen = view.world_to_screen(QPointF(*world_point)).toPoint()
    view.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, screen, Qt.NoButton,
                                    buttons, modifiers))
    return screen


def release(view, world_point, button=Qt.LeftButton, modifiers=Qt.NoModifier):
    screen = view.world_to_screen(QPointF(*world_point)).toPoint()
    view.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, screen,
                                       button, Qt.NoButton, modifiers))
    return screen


def undo_depth(host):
    return len(host.state.undo_stack)


# ---------------------------------------------------------------------------
# Direct side stretching in object mode
# ---------------------------------------------------------------------------

def test_pressing_a_side_stretches_it_without_a_handle(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    # Along the east side, clear of the corner/midpoint resize handles.
    press(view, (32, 20))
    assert host.components.drag is not None
    move(view, (64, 20))
    release(view, (64, 20))

    assert brush['size'][0] == pytest.approx(96.0)
    assert brush['size'][2] == pytest.approx(64.0)


def test_a_side_stretch_is_one_undo_step(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    before = undo_depth(host)

    press(view, (32, 20))
    for x in (40, 48, 56, 64):
        move(view, (x, 20))
    release(view, (64, 20))

    assert undo_depth(host) == before + 1


def test_a_stretch_that_never_moves_leaves_no_undo_step(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    before = undo_depth(host)

    press(view, (32, 20))
    release(view, (32, 20))

    assert undo_depth(host) == before
    assert brush['size'] == [64, 64, 64]


def test_pressing_the_middle_of_a_brush_still_moves_it(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    press(view, (0, 0))
    assert host.components.drag is None      # not a stretch
    assert view.is_dragging_object


def test_pressing_empty_space_still_starts_a_marquee(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    press(view, (400, 400))
    assert host.components.drag is None
    assert view.is_marquee_select


def test_side_stretch_is_not_offered_for_an_unselected_brush(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(None)

    press(view, (32, 20))
    assert host.components.drag is None


def test_the_existing_resize_handles_still_win_where_they_sit(editor):
    """Side stretching fills in *between* the handles; it does not replace them.

    The corner and edge-midpoint handles are checked first, so a press exactly
    on one keeps doing what it always did.
    """
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    press(view, (32, 32))                  # right on the corner handle
    assert host.components.drag is None
    assert view.is_resizing_brush
    view.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, view.world_to_screen(QPointF(32, 32)).toPoint(),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def test_escape_cancels_a_stretch_and_restores_the_brush(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    before = undo_depth(host)

    press(view, (32, 20))
    move(view, (96, 20))
    assert brush['size'][0] != 64
    view.cancel_component_drag()

    assert brush['size'] == [64, 64, 64]
    assert undo_depth(host) == before


# ---------------------------------------------------------------------------
# Component modes
# ---------------------------------------------------------------------------

def select_box(host, **kwargs):
    brush = make_box(**kwargs)
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    return brush


def test_vertex_mode_press_drags_a_corner(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (32, 32))
    assert host.components.drag is not None
    move(view, (64, 64))
    release(view, (64, 64))

    points = ce.brush_points(brush)
    assert any(abs(p[0] - 64) < 1e-6 and abs(p[2] - 64) < 1e-6 for p in points)


def test_edge_mode_press_drags_an_edge(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_EDGE)

    press(view, (32, 0))                   # midpoint of an east-side edge
    assert host.components.drag is not None
    assert host.components.drag.label == 'drag edge'
    move(view, (48, 0))
    release(view, (48, 0))
    assert bg.get_convex(brush) is not None


def test_face_mode_press_drags_the_plane(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_FACE)

    press(view, (33, 0))
    assert host.components.drag is not None
    assert host.components.drag.label == 'drag face'
    move(view, (64, 0))
    release(view, (64, 0))
    assert brush['size'][0] == pytest.approx(96.0)


def test_a_component_drag_is_one_undo_step(editor):
    host, view = editor
    select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)
    before = undo_depth(host)

    press(view, (32, 32))
    for offset in (36, 40, 48, 64):
        move(view, (offset, offset))
    release(view, (64, 64))

    assert undo_depth(host) == before + 1


def test_a_component_drag_snaps_to_the_grid(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (32, 32))
    move(view, (70, 32))                   # 70 is not on the 16 grid
    release(view, (70, 32))

    points = ce.brush_points(brush)
    assert any(abs(p[0] - 64.0) < 1e-6 for p in points)


def test_grid_snapping_can_be_switched_off(editor):
    host, view = editor
    brush = select_box(host)
    view.snap_to_grid_enabled = False
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (32, 32))
    move(view, (70, 32))
    release(view, (70, 32))

    points = ce.brush_points(brush)
    assert any(abs(p[0] - 70.0) < 1e-6 for p in points)


def test_a_press_that_misses_every_component_falls_through_to_selection(editor):
    host, view = editor
    first = select_box(host)
    second = make_box(pos=(300, 0, 0))
    host.state.brushes.append(second)
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (300, 0))                  # inside the other brush, no handle
    assert host.components.drag is None
    assert host.state.selected_object is second
    assert first is not second


def test_hovering_highlights_without_repainting_the_world(editor):
    host, view = editor
    select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)

    move(view, (32, 32), buttons=Qt.NoButton)
    assert host.components.hover is not None
    version = host.components.version
    move(view, (32, 32), buttons=Qt.NoButton)      # same handle
    assert host.components.version == version


def test_leaving_component_mode_clears_the_handles(editor):
    host, view = editor
    select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)
    move(view, (32, 32), buttons=Qt.NoButton)
    assert host.components.hover is not None

    host.set_component_mode(ce.MODE_OBJECT)
    assert host.components.hover is None
    assert host.components.selection == []


def test_cycling_walks_the_component_modes(editor):
    host, _ = editor
    order = []
    for _ in range(4):
        host.cycle_component_mode()
        order.append(host.components.mode)
    assert order == [ce.MODE_VERTEX, ce.MODE_EDGE, ce.MODE_FACE, ce.MODE_OBJECT]


def test_an_invalid_drag_keeps_the_brush_usable(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_FACE)

    press(view, (33, 0))
    move(view, (-400, 0))                  # push the east face past the west
    release(view, (-400, 0))

    convex = bg.get_shape(brush)
    assert convex is not None and convex.is_valid
    assert brush['size'][0] > 0


# ---------------------------------------------------------------------------
# Clone and place
# ---------------------------------------------------------------------------

def test_clone_placement_follows_the_cursor_and_commits_on_click(editor):
    host, view = editor
    brush = select_box(host)
    clone = make_box(pos=(16, 0, 16))
    host.state.brushes.append(clone)
    host.set_selected_objects([clone])
    host.clone_placement = {'objects': [clone], 'anchor': None}

    move(view, (100, 100), buttons=Qt.NoButton)    # latches the anchor
    move(view, (200, 100), buttons=Qt.NoButton)    # and now it follows
    # The copy travels in grid steps (96 -> 192 on a 16 grid) and keeps the
    # offset it was cloned with, so it lands at 16 + 96.
    assert clone['pos'][0] == pytest.approx(112.0)

    press(view, (200, 100))
    assert not host.clone_placement_active()
    assert clone in host.state.brushes
    assert brush in host.state.brushes


def test_clone_placement_cancel_removes_the_copy(editor):
    host, view = editor
    select_box(host)
    clone = make_box(pos=(16, 0, 16))
    host.state.brushes.append(clone)
    host.clone_placement = {'objects': [clone], 'anchor': None}

    assert host.cancel_clone_placement()
    assert clone not in host.state.brushes
    assert not host.clone_placement_active()


def test_clone_placement_snaps_to_the_grid(editor):
    host, view = editor
    clone = make_box(pos=(0, 0, 0))
    host.state.brushes.append(clone)
    host.clone_placement = {'objects': [clone], 'anchor': None}

    move(view, (0, 0), buttons=Qt.NoButton)
    move(view, (70, 0), buttons=Qt.NoButton)
    assert clone['pos'][0] == pytest.approx(64.0)   # snapped, not 70


# ---------------------------------------------------------------------------
# Radiant-style area selections, driven through the real MainWindow methods
# ---------------------------------------------------------------------------

def area_scene(host):
    region = make_box(pos=(0, 0, 0), size=(128, 128, 128))
    inside = make_box(pos=(0, 0, 0), size=(32, 32, 32))
    overlapping = make_box(pos=(80, 0, 0), size=(64, 64, 64))
    above = make_box(pos=(0, 400, 0), size=(32, 32, 32))
    host.state.brushes.extend([region, inside, overlapping, above])
    host.set_selected_object(region)
    return region, inside, overlapping, above


def test_select_touching_keeps_the_region_brush(editor):
    host, _ = editor
    region, inside, overlapping, _ = area_scene(host)
    host.select_touching()
    selected = host.state.selected_objects
    assert region in selected
    assert inside in selected
    assert overlapping in selected
    assert region in host.state.brushes


def test_select_inside_consumes_the_region_brush(editor):
    host, _ = editor
    region, inside, overlapping, _ = area_scene(host)
    host.select_inside()
    assert region not in host.state.brushes
    assert host.state.selected_objects == [inside]
    assert overlapping not in host.state.selected_objects


def test_select_complete_tall_reaches_up_the_column(editor):
    host, _ = editor
    region, inside, _, above = area_scene(host)
    host.select_complete_tall()
    assert set(map(id, host.state.selected_objects)) == {id(inside), id(above)}


def test_select_partial_tall_includes_partial_overlap(editor):
    host, _ = editor
    region, inside, overlapping, above = area_scene(host)
    host.select_partial_tall()
    assert set(map(id, host.state.selected_objects)) == {
        id(inside), id(overlapping), id(above)}


def test_area_selection_needs_exactly_one_brush(editor):
    host, _ = editor
    a = make_box(pos=(0, 0, 0))
    b = make_box(pos=(200, 0, 0))
    host.state.brushes.extend([a, b])
    host.set_selected_objects([a, b])
    host.select_inside()
    assert a in host.state.brushes and b in host.state.brushes
    assert any('exactly one' in t for t in host.toasts)


def test_area_selection_skips_hidden_geometry(editor):
    host, _ = editor
    region, inside, _, _ = area_scene(host)
    ghost = make_box(pos=(0, 0, 0), size=(8, 8, 8), hidden=True)
    host.state.brushes.append(ghost)
    host.select_inside()
    assert ghost not in host.state.selected_objects


# ---------------------------------------------------------------------------
# CSG workflow: one undo step per operation
# ---------------------------------------------------------------------------

def test_subtract_is_one_undo_step(editor):
    host, _ = editor
    host.perform_subtraction = types.MethodType(MainWindow.perform_subtraction, host)
    target = make_box(pos=(0, 0, 0), size=(256, 64, 256))
    cutter = make_box(pos=(0, 0, 0), size=(64, 64, 64))
    host.state.brushes.extend([target, cutter])
    host.set_selected_object(cutter)
    before = undo_depth(host)

    host.perform_subtraction()

    assert undo_depth(host) == before + 1
    assert target not in host.state.brushes          # replaced by fragments
    assert len(host.state.brushes) > 2


def test_subtract_can_fold_into_a_caller_s_undo_step(editor):
    """Hollow opens one checkpoint and runs a subtract inside it."""
    host, _ = editor
    host.perform_subtraction = types.MethodType(MainWindow.perform_subtraction, host)
    target = make_box(pos=(0, 0, 0), size=(256, 64, 256))
    cutter = make_box(pos=(0, 0, 0), size=(64, 64, 64))
    host.state.brushes.extend([target, cutter])
    host.set_selected_object(cutter)

    host.save_state()                                # the caller's checkpoint
    before = undo_depth(host)
    host.perform_subtraction(push_undo=False)

    assert undo_depth(host) == before                # no second checkpoint


# ---------------------------------------------------------------------------
# Cache isolation: an edit must not disturb brushes it did not touch
# ---------------------------------------------------------------------------

def test_a_component_edit_only_invalidates_the_edited_brush(editor):
    host, view = editor
    edited = make_box(pos=(0, 0, 0))
    untouched = make_box(pos=(400, 0, 0))
    host.state.brushes.extend([edited, untouched])
    host.set_selected_object(edited)
    bg.box_to_geometry(untouched)
    other_cache = bg.get_convex(untouched)

    host.set_component_mode(ce.MODE_VERTEX)
    press(view, (32, 32))
    move(view, (64, 64))
    release(view, (64, 64))

    # Same cached geometry object: the neighbour was never rebuilt.
    assert bg.get_convex(untouched) is other_cache


def test_hovering_does_not_rebuild_any_geometry(editor):
    host, view = editor
    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    host.set_component_mode(ce.MODE_VERTEX)

    shape = bg.get_shape(brush)
    for point in ((32, 32), (0, 0), (-32, 32), (32, -32)):
        move(view, point, buttons=Qt.NoButton)
    assert bg.get_shape(brush) is shape


def test_component_picking_ignores_unselected_brushes(editor):
    host, view = editor
    selected = make_box(pos=(0, 0, 0))
    ignored = make_box(pos=(0, 0, 0), size=(16, 16, 16))
    host.state.brushes.extend([selected, ignored])
    host.set_selected_object(selected)
    host.set_component_mode(ce.MODE_VERTEX)

    move(view, (8, 8), buttons=Qt.NoButton)          # on `ignored`'s corner
    assert host.components.hover is None             # it is not selected


def test_locked_and_hidden_brushes_are_never_component_targets(editor):
    host, _ = editor
    locked = make_box(lock=True)
    hidden = make_box(hidden=True)
    host.state.brushes.extend([locked, hidden])
    host.set_selected_objects([locked, hidden])
    assert host.component_drag_targets() == []


def test_shift_click_adds_a_second_component_to_the_drag(editor):
    host, view = editor
    brush = select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (32, 32))
    release(view, (32, 32))
    assert len(host.components.selection) == 1

    press(view, (-32, 32), modifiers=Qt.ShiftModifier)
    assert len(host.components.selection) == 2
    move(view, (-32, 48), modifiers=Qt.ShiftModifier)
    release(view, (-32, 48), modifiers=Qt.ShiftModifier)

    points = ce.brush_points(brush)
    # Both grabbed corners travelled together.
    assert sum(1 for p in points if abs(p[2] - 48) < 1e-6) >= 2


def test_shift_clicking_a_selected_component_deselects_without_dragging(editor):
    host, view = editor
    select_box(host)
    host.set_component_mode(ce.MODE_VERTEX)

    press(view, (32, 32))
    release(view, (32, 32))
    assert len(host.components.selection) == 1

    press(view, (32, 32), modifiers=Qt.ShiftModifier)
    assert host.components.selection == []
    assert host.components.drag is None


# ---------------------------------------------------------------------------
# Free rotate: hold and drag, Radiant style
# ---------------------------------------------------------------------------

def enter_rotate(host, view):
    host.rotate_mode = True
    view.rotate_snap_deg = 15.0


def test_rotate_drag_spins_the_selection(editor):
    host, view = editor
    brush = select_box(host)
    enter_rotate(host, view)

    press(view, (64, 0))                   # grab at 0 degrees from the centre
    assert view.rotate_dragging
    move(view, (0, 64))                    # swing round to 90
    release(view, (0, 64))

    assert not view.rotate_dragging
    assert bg.brush_has_geometry(brush)     # a rotated brush carries planes


def test_rotate_needs_no_drag_to_be_a_no_op(editor):
    host, view = editor
    brush = select_box(host)
    enter_rotate(host, view)
    before = undo_depth(host)

    press(view, (64, 0))
    release(view, (64, 0))

    assert undo_depth(host) == before       # no empty step in the history
    assert brush['size'] == [64, 64, 64]


def test_a_whole_rotate_drag_is_one_undo_step(editor):
    host, view = editor
    select_box(host)
    enter_rotate(host, view)
    before = undo_depth(host)

    press(view, (64, 0))
    for point in ((60, 20), (45, 45), (20, 60), (0, 64)):
        move(view, point)
    release(view, (0, 64))

    assert undo_depth(host) == before + 1


def test_rotate_snaps_to_fifteen_degree_steps_with_the_grid_on(editor):
    host, view = editor
    select_box(host)
    view.snap_to_grid_enabled = True
    enter_rotate(host, view)

    press(view, (64, 0))
    move(view, (62, 8))                     # roughly 7 degrees round
    assert view.rotate_applied % 15.0 == pytest.approx(0.0)
    release(view, (62, 8))


def test_rotate_is_free_with_the_grid_off(editor):
    host, view = editor
    select_box(host)
    view.snap_to_grid_enabled = False
    enter_rotate(host, view)

    press(view, (64, 0))
    move(view, (55, 32))
    assert view.rotate_applied % 15.0 != pytest.approx(0.0)
    release(view, (55, 32))


def test_rotate_spins_a_multi_selection_about_one_pivot(editor):
    """Two brushes turn as one body: they swap places, not spin on the spot."""
    host, view = editor
    left = make_box(pos=(-128, 0, 0))
    right = make_box(pos=(128, 0, 0))
    host.state.brushes.extend([left, right])
    host.set_selected_objects([left, right])
    enter_rotate(host, view)

    press(view, (256, 0))                   # 0 degrees about the shared centre
    move(view, (0, 256))                    # swing to 90
    release(view, (0, 256))

    # The pair has rotated about the origin, so they now straddle the z axis.
    assert abs(left['pos'][0]) < 1.0
    assert abs(right['pos'][0]) < 1.0
    assert left['pos'][2] == pytest.approx(-right['pos'][2], abs=1.0)


def test_rotate_carries_entities_round_with_the_brushes(editor):
    """An entity in the selection orbits the pivot instead of being left behind."""
    host, view = editor
    from editor.things import Light
    brush = make_box(pos=(-128, 0, 0))
    light = Light(pos=[128, 0, 0])
    host.state.brushes.append(brush)
    host.state.things.append(light)
    host.set_selected_objects([brush, light])
    enter_rotate(host, view)

    pivot = host.selection_centre()
    reach = abs(float(light.pos[0]) - pivot[0])

    press(view, (pivot[0] + 256, pivot[2]))
    move(view, (pivot[0], pivot[2] + 256))          # a quarter turn
    release(view, (pivot[0], pivot[2] + 256))

    # A quarter turn takes the light's offset from along x to along z, at the
    # same distance from the pivot it started at.
    assert float(light.pos[0]) == pytest.approx(pivot[0], abs=1.0)
    assert abs(float(light.pos[2]) - pivot[2]) == pytest.approx(reach, abs=1.0)


def test_escape_cancels_a_rotate_and_puts_the_angle_back(editor):
    host, view = editor
    brush = select_box(host)
    enter_rotate(host, view)
    before = undo_depth(host)

    press(view, (64, 0))
    move(view, (0, 64))
    view.cancel_rotate()

    assert not view.rotate_dragging
    assert undo_depth(host) == before
    lo, hi = ce.object_bounds(brush)
    assert (hi - lo)[0] == pytest.approx(64.0, abs=1e-3)
    assert (hi - lo)[2] == pytest.approx(64.0, abs=1e-3)


def test_rotate_with_nothing_selected_says_so(editor):
    host, view = editor
    host.set_selected_object(None)
    enter_rotate(host, view)

    press(view, (64, 0))
    assert not view.rotate_dragging
    assert any('select something' in t.lower() for t in host.toasts)


# ---------------------------------------------------------------------------
# Cloning entities
# ---------------------------------------------------------------------------

def test_cloning_an_entity_makes_an_independent_copy(editor):
    host, _ = editor
    from editor.things import Light
    light = Light(pos=[64, 32, 0])
    light.properties['radius'] = 350
    light.properties['colour'] = [1.0, 0.5, 0.25]
    host.state.things.append(light)

    clone = light.duplicate()

    assert clone is not light
    assert clone.properties is not light.properties
    assert clone.properties['radius'] == 350
    assert clone.properties['colour'] == [1.0, 0.5, 0.25]
    # Editing one must not reach the other.
    clone.properties['radius'] = 99
    assert light.properties['radius'] == 350


def test_a_cloned_entity_gets_its_own_identity(editor):
    host, _ = editor
    from editor.things import Light
    light = Light(pos=[0, 0, 0])
    light.properties['name'] = 'lamp'
    clone = light.duplicate()

    assert clone.properties['id'] != light.properties['id']
    assert clone.properties['name'] == 'lamp (copy)'


def test_repeated_clones_do_not_share_a_name(editor):
    from editor.things import Light
    light = Light(pos=[0, 0, 0])
    light.properties['name'] = 'lamp'
    taken = {'lamp'}
    names = []
    for _ in range(3):
        clone = light.duplicate(existing_names=taken)
        names.append(clone.properties['name'])
        taken.add(clone.properties['name'])
    assert names == ['lamp (copy)', 'lamp (copy 2)', 'lamp (copy 3)']
    assert len(set(names)) == 3


def test_a_cloned_entity_has_its_own_position(editor):
    from editor.things import Light
    light = Light(pos=[10, 20, 30])
    clone = light.duplicate()
    clone.pos[0] = 999
    assert light.pos[0] == 10


def test_shift_space_clones_entities_as_well_as_brushes(editor):
    host, _ = editor
    from editor.things import Light
    host.clone_selected_object = types.MethodType(
        MainWindow.clone_selected_object, host)
    host.right_tabs = _StubTabs(host.view_top)
    host.grid_size_spinbox = _StubSpin(16)

    light = Light(pos=[0, 0, 0])
    light.properties['name'] = 'lamp'
    brush = make_box()
    host.state.things.append(light)
    host.state.brushes.append(brush)
    host.set_selected_objects([brush, light])

    host.clone_selected_object()

    assert len(host.state.things) == 2
    assert len(host.state.brushes) == 2
    new_light = host.state.things[1]
    assert new_light is not light
    assert new_light.properties['name'] == 'lamp (copy)'
    assert new_light.properties['id'] != light.properties['id']
    # Both copies are what is now selected and being placed.
    assert host.clone_placement_active()
    assert len(host.clone_placement['objects']) == 2


def test_cloning_a_named_brush_gives_the_copy_its_own_name(editor):
    host, _ = editor
    host.clone_selected_object = types.MethodType(
        MainWindow.clone_selected_object, host)
    host.right_tabs = _StubTabs(host.view_top)
    host.grid_size_spinbox = _StubSpin(16)

    brush = make_box()
    brush['name'] = 'pillar'
    brush['id'] = 'original-id'
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    host.clone_selected_object()

    copy_brush = host.state.brushes[1]
    assert copy_brush['name'] == 'pillar (copy)'
    assert copy_brush['id'] != 'original-id'


def test_cloning_an_unnamed_brush_does_not_invent_a_name(editor):
    host, _ = editor
    host.clone_selected_object = types.MethodType(
        MainWindow.clone_selected_object, host)
    host.right_tabs = _StubTabs(host.view_top)
    host.grid_size_spinbox = _StubSpin(16)

    brush = make_box()
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    host.clone_selected_object()
    assert 'name' not in host.state.brushes[1]


# ---------------------------------------------------------------------------
# Clip and split
# ---------------------------------------------------------------------------

def key(view, code, modifiers=Qt.NoModifier):
    from PyQt5.QtGui import QKeyEvent
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, code, modifiers))


def place_cut(view, a, b):
    """Put the two clip points down, splitting the view along x = a[0]."""
    view.clip_points = [QPointF(*a), QPointF(*b)]
    view.clip_hover = QPointF(a[0] + 64, a[1])
    view._update_clip_keep_side()


def test_plain_enter_keeps_one_side(editor):
    host, view = editor
    brush = make_box(size=(128, 64, 128))
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    host.clip_mode = True

    place_cut(view, (0, -128), (0, 128))
    key(view, Qt.Key_Return)

    assert len(host.state.brushes) == 1
    assert brush['size'][0] == pytest.approx(64.0)


def test_shift_enter_keeps_both_sides(editor):
    host, view = editor
    brush = make_box(size=(128, 64, 128))
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    host.clip_mode = True

    place_cut(view, (0, -128), (0, 128))
    key(view, Qt.Key_Return, Qt.ShiftModifier)

    assert len(host.state.brushes) == 2
    halves = host.state.brushes
    assert all(h['size'][0] == pytest.approx(64.0) for h in halves)
    # One piece each side of the cut.
    assert halves[0]['pos'][0] == pytest.approx(-halves[1]['pos'][0])


def test_a_split_keeps_both_halves_selected(editor):
    host, view = editor
    brush = make_box(size=(128, 64, 128))
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    host.clip_mode = True

    place_cut(view, (0, -128), (0, 128))
    key(view, Qt.Key_Return, Qt.ShiftModifier)

    assert len(host.state.selected_objects) == 2


def test_a_split_is_one_undo_step(editor):
    host, view = editor
    brush = make_box(size=(128, 64, 128))
    host.state.brushes.append(brush)
    host.set_selected_object(brush)
    host.clip_mode = True
    before = undo_depth(host)

    place_cut(view, (0, -128), (0, 128))
    key(view, Qt.Key_Return, Qt.ShiftModifier)

    assert undo_depth(host) == before + 1


def test_the_new_half_gets_its_own_identity(editor):
    host, _ = editor
    brush = make_box(size=(128, 64, 128))
    brush['name'] = 'pillar'
    brush['id'] = 'original-id'
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False, split=True)

    other = next(b for b in host.state.brushes if b is not brush)
    assert other['name'] == 'pillar (copy)'
    assert other['id'] != 'original-id'
    assert brush['name'] == 'pillar'          # the original keeps its own


def test_a_split_carries_the_brush_properties_to_both_halves(editor):
    host, _ = editor
    brush = make_box(size=(128, 64, 128))
    brush['is_trigger'] = True
    brush['colour'] = [0.2, 0.4, 0.6]
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False, split=True)

    for piece in host.state.brushes:
        assert piece['is_trigger'] is True
        assert piece['colour'] == [0.2, 0.4, 0.6]


def test_a_plane_that_misses_the_brush_makes_no_second_half(editor):
    host, _ = editor
    brush = make_box(size=(128, 64, 128))
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    host.apply_clip_to_selection([1.0, 0.0, 0.0], 5000.0, False, split=True)

    assert len(host.state.brushes) == 1


def test_splitting_a_multi_selection_splits_each_brush(editor):
    host, _ = editor
    a = make_box(pos=(0, 0, 0), size=(128, 64, 128))
    b = make_box(pos=(0, 0, 400), size=(128, 64, 128))
    host.state.brushes.extend([a, b])
    host.set_selected_objects([a, b])

    count = host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False, split=True)

    assert count == 2
    assert len(host.state.brushes) == 4


def test_the_new_half_sits_next_to_its_original_in_the_scene(editor):
    host, _ = editor
    a = make_box(pos=(0, 0, 0), size=(128, 64, 128))
    tail = make_box(pos=(0, 0, 900))
    host.state.brushes.extend([a, tail])
    host.set_selected_object(a)

    host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False, split=True)

    assert host.state.brushes.index(a) == 0
    assert host.state.brushes[1] is not tail      # the half, not the far brush
    assert host.state.brushes[2] is tail


def test_split_works_on_an_already_angled_brush(editor):
    host, _ = editor
    brush = make_box(size=(128, 128, 128))
    assert bg.clip_brush(brush, [0.0, 1.0, 1.0], 0.0)     # make it a wedge
    host.state.brushes.append(brush)
    host.set_selected_object(brush)

    count = host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False, split=True)

    assert count == 1
    assert len(host.state.brushes) == 2
    for piece in host.state.brushes:
        convex = bg.get_convex(piece)
        assert convex is not None and convex.is_valid


def test_nothing_selected_leaves_the_undo_history_alone(editor):
    host, _ = editor
    host.set_selected_object(None)
    before = undo_depth(host)
    assert host.apply_clip_to_selection([1.0, 0.0, 0.0], 0.0, False,
                                        split=True) == 0
    assert undo_depth(host) == before
