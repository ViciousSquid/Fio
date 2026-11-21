"""What a 2D view repaints, and how much of the map a repaint walks.

Measured on a 20 000-brush map (offscreen, 800x600 top view): a repaint
took 113 ms zoomed out and 38 ms zoomed in with almost nothing in view,
and the view repainted on every bare mouse move and, in all three 2D views,
on every frame the 3D camera moved. Now a bare hover repaints nothing; a
camera move draws the kept image of the scene and the cone over it; and a
zoomed-in repaint tests only the brushes an array of their bounds puts in
view. Each guard counts work done, not time, and checks the frame is the one
a full repaint draws.
"""

import pytest
from PyQt5.QtCore import QEvent, QPointF, Qt
from PyQt5.QtGui import QMouseEvent

from editor import component_edit as ce
from editor.view_2d import View2D
from tests.helpers.worlds import box_brush

pytestmark = [pytest.mark.qt, pytest.mark.perf]

SIDE = 40                   # a SIDE x SIDE grid of brushes, 80 units apart
SPACING = 80.0


@pytest.fixture
def grid(main_window):
    w = main_window
    w.state.brushes = [box_brush('b%d' % i, ((i % SIDE) * SPACING, 0.0,
                                             (i // SIDE) * SPACING))
                       for i in range(SIDE * SIDE)]
    w.state.mark_world_changed()
    w.set_tool_mode('select')
    view = w.view_top
    view.show()
    view.resize(400, 300)
    view.zoom_factor = 1.0
    view.pan_offset = QPointF(SIDE * SPACING / 2, SIDE * SPACING / 2)
    view.update()
    return w, view


def _count(monkeypatch, name):
    """Calls of View2D.<name> from now on (still doing their work)."""
    calls = []
    real = getattr(View2D, name)

    def counted(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(View2D, name, counted)
    return calls


def _full_frame(view):
    view.update()
    return view.grab().toImage()


def _hover(view, x, y):
    view.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(x, y), Qt.NoButton,
                                    Qt.NoButton, Qt.NoModifier))


# -- a bare hover --------------------------------------------------------------

def test_a_bare_hover_asks_for_no_repaint(grid, monkeypatch):
    w, view = grid
    w.set_selected_objects([w.state.brushes[0]])    # handles to hover near
    updates = _count(monkeypatch, 'update')
    overlays = _count(monkeypatch, '_request_overlay_frame')
    for i in range(20):
        _hover(view, 100 + 7 * i, 80 + 3 * i)
    assert (updates, overlays) == ([], [])


def test_a_component_highlight_moves_without_painting_the_scene(grid, monkeypatch):
    w, view = grid
    brush = w.state.brushes[SIDE * (SIDE // 2) + SIDE // 2]     # mid-view
    w.set_selected_objects([brush])
    w.components.set_mode(ce.MODE_VERTEX)
    view.update()
    view.grab()
    scenes = _count(monkeypatch, '_paint_scene_layer')
    updates = _count(monkeypatch, 'update')
    corner = view.world_to_screen(QPointF(brush['pos'][0] - 32, brush['pos'][2] - 32))
    _hover(view, corner.x(), corner.y())
    assert w.components.hover is not None, "the hover reached a vertex"
    assert updates == []
    hovered = view.grab().toImage()
    assert scenes == [], "the handles are drawn over the kept scene"
    assert hovered == _full_frame(view)


# -- the 3D camera ---------------------------------------------------------------

def test_a_camera_move_keeps_the_scene_and_moves_the_cone(grid, monkeypatch):
    w, view = grid
    camera = w.view_3d.camera
    camera.pos.x, camera.pos.z = view.pan_offset.x(), view.pan_offset.y()
    view._last_camera_pos = None
    before = _full_frame(view)
    scenes = _count(monkeypatch, '_paint_scene_layer')
    camera.pos.x += 48.0
    camera.yaw += 40.0
    view._check_camera_changed()
    moved = view.grab().toImage()
    assert scenes == [], "a camera move painted the scene again"
    assert moved != before, "the cone did not move"
    assert moved == _full_frame(view)
    assert len(scenes) == 1


def test_any_change_to_the_scene_paints_it_afresh_on_a_camera_frame(grid, monkeypatch):
    """An edit that only bumps the world's epoch (no update() call) is not
    hidden behind the kept image when the camera moves next."""
    w, view = grid
    view.grab()
    scenes = _count(monkeypatch, '_paint_scene_layer')
    brush = w.state.brushes[0]
    w.state.save_state()
    brush['pos'][0] += 16.0
    w.view_3d.camera.pos.x += 10.0
    view._check_camera_changed()
    view.grab()
    assert len(scenes) == 1


# -- the brush cull --------------------------------------------------------------

def _all_brushes(self, visible_bounds, axis1_idx, axis2_idx):
    """The cull as it was: every brush in the map, in map order."""
    return list(self.editor.state.brushes)


def test_a_zoomed_in_repaint_tests_only_the_brushes_in_view(grid, monkeypatch):
    w, view = grid
    view.zoom_factor = 2.0                          # 200 x 150 units in view
    tested = _count(monkeypatch, 'is_brush_visible')
    culled = _full_frame(view)
    tested = len(tested)
    in_view = view.get_visible_world_bounds()
    visible = [b for b in w.state.brushes
               if View2D.is_brush_visible(view, b, in_view, 0, 2)]
    assert 0 < len(visible) <= 12
    assert tested <= len(visible) + 8, \
        "%d of %d brushes tested for %d in view" % (tested, SIDE * SIDE, len(visible))
    monkeypatch.setattr(View2D, '_brushes_in_view', _all_brushes)
    assert culled == _full_frame(view)


def test_brushes_written_in_place_after_the_checkpoint_are_still_drawn(grid, monkeypatch):
    """A tool writes what it edits for many frames after one undo checkpoint
    (one world epoch): the selection, its group mates and an object a drag
    grabbed outside the selection. Each is drawn where it is now, though the
    cull's array has it where it was."""
    w, view = grid
    brushes = w.state.brushes
    selected, mate, grabbed = brushes[0], brushes[1], brushes[2]   # off-view
    selected['brush_group'] = mate['brush_group'] = 'pair'
    w.set_selected_objects([selected])
    view.drag_primary = grabbed
    w.state.save_state()
    view.grab()                                     # the array is built
    centre = (view.pan_offset.x(), view.pan_offset.y())
    for n, brush in enumerate((selected, mate, grabbed)):
        brush['pos'][0] = centre[0] - 100.0 + 90.0 * n
        brush['pos'][2] = centre[1] + 40.0
    culled = _full_frame(view)
    monkeypatch.setattr(View2D, '_brushes_in_view', _all_brushes)
    assert culled == _full_frame(view)
