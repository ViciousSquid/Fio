"""Light volumes (editor.things.LightVolume), phase 1: authoring.

Made from a brush in a 2D view, saved and loaded, edited with handles in 2D,
drawn and picked in 3D (no icon anywhere), edited in the property editor and
with the transform gizmo. Phase 1 adds no lighting: a map's point lights are
untouched and a light volume does not take a light slot.
"""

import copy

import glm
import pytest
from PyQt5.QtCore import QPointF

from editor import light_volume_handles as lvh
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt


def _volume(lo=(-64, 0, -32), hi=(64, 128, 32), **props):
    from editor.things import LightVolume
    return LightVolume.from_bounds(lo, hi, **props)


# -- the entity -----------------------------------------------------------------

def test_from_bounds_fills_the_box_with_the_source_at_its_centre():
    v = _volume()
    assert list(v.pos) == [0.0, 64.0, 0.0]
    assert v.get_volume_size() == [128.0, 128.0, 64.0]
    assert v.get_bounds() == ([-64.0, 0.0, -32.0], [64.0, 128.0, 32.0])
    assert v.get_source_position() == [0.0, 64.0, 0.0]


def test_a_volume_never_collapses_below_its_minimum_size():
    from editor.things import LightVolume
    v = _volume((0, 0, 0), (100, 0, 100))
    assert v.get_volume_size()[1] == LightVolume.MIN_SIZE
    v.properties['volume_size'] = ['x', None, 3]
    assert v.get_volume_size() == [256.0, 256.0, 256.0]       # unreadable: default


def test_a_light_volume_has_no_icon():
    from editor.things import LightVolume
    assert LightVolume.pixmap_path is None
    assert _volume().get_icon_pixmap() is None


# -- made from a brush, undone, saved, loaded ------------------------------------

def test_light_volume_from_a_brush_replaces_it_in_one_undo(main_window):
    from editor.things import LightVolume
    brush = box_brush("room", (32, 64, 0), (256, 128, 192))
    main_window.state.brushes[:] = [brush]
    main_window.state.things[:] = []
    volume = main_window.view_top.create_light_volume_from_brush(brush)
    assert isinstance(volume, LightVolume)
    assert main_window.state.brushes == []
    assert main_window.state.things == [volume]
    assert main_window.state.selected_objects == [volume]
    assert volume.get_bounds() == ([-96.0, 0.0, -96.0], [160.0, 128.0, 96.0])
    main_window.undo()
    assert [b['name'] for b in main_window.state.brushes] == ['room']
    assert main_window.state.things == []


def test_a_locked_brush_is_not_replaced(main_window):
    brush = box_brush("room", (0, 0, 0), (64, 64, 64), lock=True)
    main_window.state.brushes[:] = [brush]
    main_window.state.things[:] = []
    assert main_window.view_top.create_light_volume_from_brush(brush) is None
    assert main_window.state.brushes == [brush]


def test_a_light_volume_survives_save_and_load(main_window):
    from editor.things import LightVolume
    volume = _volume(source_offset=[10.0, 20.0, -30.0], intensity=2.5,
                     colour=[255, 128, 0], casts_shadows=True, shadow_softness=0.5)
    state = main_window.state
    state.brushes[:] = []
    state.things[:] = [volume]
    data = copy.deepcopy(state.get_level_data())
    state.things[:] = []
    state.load_from_data(data, save_undo=False)
    (loaded,) = state.things
    assert isinstance(loaded, LightVolume)
    assert loaded.get_bounds() == volume.get_bounds()
    assert loaded.get_source_offset() == [10.0, 20.0, -30.0]
    for key in ('intensity', 'colour', 'casts_shadows', 'shadow_softness', 'type'):
        assert loaded.properties[key] == volume.properties[key], key


# -- 2D handles --------------------------------------------------------------------

def test_a_corner_handle_moves_its_sides_and_keeps_the_opposite_ones():
    v = _volume((0, 0, 0), (100, 50, 100))
    pos, size = lvh.resize(v, (1, 1), (160, 120), 0, 2)          # top view: X, Z
    assert size == [160.0, 50.0, 120.0] and pos == [80.0, 25.0, 60.0]


def test_a_side_dragged_past_its_opposite_swaps_and_respects_the_minimum():
    v = _volume((0, 0, 0), (100, 50, 100))
    pos, size = lvh.resize(v, (1, 0), (-40, 0), 0, 2)
    assert size[0] == 40.0 and pos[0] == -20.0                    # swapped
    pos, size = lvh.resize(v, (1, 0), (2, 0), 0, 2)
    assert size[0] == v.MIN_SIZE                                  # never thinner


def test_moving_the_source_keeps_its_depth_and_snaps():
    v = _volume((0, 0, 0), (100, 100, 100), source_offset=[0.0, 7.0, 0.0])
    assert lvh.move_source(v, (77, 13), 0, 2, grid=16) == [30.0, 7.0, -34.0]


def test_in_2d_a_volume_is_picked_by_its_outline_or_source_never_its_inside(main_window):
    v = _volume((-128, 0, -128), (128, 128, 128), source_offset=[64.0, 0.0, 64.0])
    brush = box_brush("crate", (0, 64, 0), (32, 32, 32))
    main_window.state.things[:] = [v]
    main_window.state.brushes[:] = [brush]
    view = main_window.view_top
    view.resize(600, 600)
    view.zoom_factor = 1.0
    view.pan_offset = QPointF(0.0, 0.0)
    edge = view.world_to_screen(QPointF(128.0, 0.0))
    assert view.get_object_at(edge) is v
    source = view.world_to_screen(QPointF(64.0, 64.0))
    assert view.get_object_at(source) is v
    middle = view.world_to_screen(QPointF(0.0, 0.0))
    assert view.get_object_at(middle) is brush                    # the brush inside
    empty = view.world_to_screen(QPointF(-60.0, 60.0))
    assert view.get_object_at(empty) is None


def test_the_2d_views_draw_no_icon_for_a_volume(main_window, monkeypatch):
    v = _volume()
    main_window.state.things[:] = [v]
    main_window.state.brushes[:] = []
    view = main_window.view_top
    sprites = []
    monkeypatch.setattr(view, '_sprite_rect', lambda pixmap, pos: sprites.append(pixmap))
    drawn = []
    original = view.draw_light_volumes
    monkeypatch.setattr(view, 'draw_light_volumes',
                        lambda painter: drawn.append(True) or original(painter))
    view.grab()
    assert drawn and not sprites


# -- 3D ------------------------------------------------------------------------------

def _aim(view, eye, target):
    view.resize(800, 600)
    view.view_matrix = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    view.projection_matrix = glm.perspective(glm.radians(70.0), 800 / 600, 0.1, 10000.0)
    view.camera.pos = glm.vec3(*eye)
    view.logic_thread.camera.get_editor_camera().pos = glm.vec3(*eye)


def test_in_3d_a_volume_is_picked_by_its_light_source_not_its_box(main_window):
    v = _volume((-128, 0, -128), (128, 256, 128), source_offset=[0.0, 96.0, 0.0])
    state = main_window.state
    state.things[:] = [v]
    state.brushes[:] = []
    view = main_window.view_3d
    _aim(view, (600.0, 300.0, 600.0), (0.0, 128.0, 0.0))
    project = view.gizmo._project_fn()
    sx, sy = project(v.get_source_position())
    assert view.picking.get_object_at_3d(sx, sy) is v
    cx, cy = project(v.pos)                     # the box's centre, off the source
    assert view.picking.get_object_at_3d(cx, cy) is None


def test_the_3d_view_draws_the_box_and_source(main_window):
    from engine import view_light_volumes
    v = _volume()
    main_window.state.things[:] = [v]
    assert view_light_volumes.light_volumes(main_window.view_3d) == [v]
    assert len(view_light_volumes.box_edges(v)) == 12
    v.properties['hidden'] = True
    assert view_light_volumes.light_volumes(main_window.view_3d) == []


# -- property editor and gizmo -------------------------------------------------------

def test_the_property_editor_edits_the_box_and_source_as_xyz(main_window):
    from PyQt5.QtWidgets import QLabel, QLineEdit
    v = _volume()
    main_window.state.things[:] = [v]
    main_window.set_selected_objects([v])
    panel = main_window.property_editor
    panel.set_object(v)
    labels = {w.text() for w in panel.findChildren(QLabel)}
    assert {"Volume Size:", "Light Source:", "Shadow Softness:"} <= labels
    panel.update_object_prop('volume_size', [64.0, 32.0, 16.0])
    assert v.get_volume_size() == [64.0, 32.0, 16.0]
    assert not any(e.text().startswith('[') for e in panel.findChildren(QLineEdit))


def test_the_scale_gizmo_resizes_the_volume_on_the_grid(main_window):
    from editor import transform_gizmo as tg
    v = _volume((-64, 0, -64), (64, 128, 64))
    main_window.state.things[:] = [v]
    main_window.state.brushes[:] = []
    main_window.set_selected_objects([v])
    main_window.grid_size_spinbox.setValue(16)
    view = main_window.view_3d
    _aim(view, (420.0, 300.0, 520.0), tuple(v.pos))
    gizmo = view.gizmo
    gizmo.set_mode(tg.SCALE)
    lay = gizmo.layout()
    cx, cy = lay.centre_px
    r = tg.VIEW_RING * tg.RADIUS_PX
    assert gizmo.press(cx + r, cy)
    gizmo.drag_to(cx + r * 1.5, cy)
    gizmo.release()
    size = v.get_volume_size()
    assert size[0] > 128 and all(s % 16 == 0 for s in size), size
    main_window.undo()
    assert main_window.state.things[0].get_volume_size() == [128.0, 128.0, 128.0]


def test_a_light_volume_takes_no_light_slot():
    """Phase 1 lights nothing: the renderer's lights are the map's Lights."""
    from engine.entity_table import EntityTable
    v = _volume(casts_shadows=True)
    table = EntityTable()
    table.begin_frame([v], epoch=1)
    assert not table.light_enabled[:table.count].any()
