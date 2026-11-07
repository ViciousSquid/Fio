"""Editor tools keep each face's one material record (Stage 1).

Every tool that makes, cuts or rebuilds brushes goes through
``engine.brush_faces``, so a face's material -- texture, scale, shift,
angle, Natural -- survives the operation in its one home: CSG subtract
fragments and hollow shells take the whole record (not only the texture),
the clip tool keeps the surviving faces' records and lets the cut face
inherit, undo/redo restore it exactly, autocaulk writes a box face's record
and leaves angled brushes alone, and the trigger toggle textures every face.
"""

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from editor import face_texture as ft  # noqa: E402
from engine import brush_faces as bf  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush  # noqa: E402

pytestmark = pytest.mark.qt

MATERIAL = dict(
    textures={tag: 'tex_%s.png' % tag for tag in bf.FACE_TAGS},
    uv_scale={'top': [2.0, 0.5], 'north': [3.0, 3.0]},
    uv_shift={'top': [0.25, 0.5], 'west': [0.1, 0.0]},
    uv_angle={'top': 30.0, 'east': 90.0},
    uv_natural={'south': True},
)


def _records(brush):
    return {key: bf.face_material(brush, key) for key in bf.face_keys(brush)}


def _box(name, pos, size, **extra):
    import copy
    brush = box_brush(name, pos, size, **copy.deepcopy(MATERIAL))
    brush.update(extra)
    return brush


def test_csg_fragments_take_every_field_of_the_brush_they_came_from(main_window):
    host = main_window
    target = _box("target", (0, 0, 0), (256, 64, 256), texture_tiling=True)
    cutter = box_brush("cutter", (0, 0, 0), (64, 64, 64))
    expected = _records(target)
    host.state.brushes.extend([target, cutter])
    host.set_selected_objects([cutter])
    host.perform_subtraction()
    fragments = [b for b in host.state.brushes if b is not cutter
                 and b.get('operation') != 'subtract']
    assert fragments and target not in fragments
    for fragment in fragments:
        assert bs_records(fragment) == expected, fragment.get('name')
        assert fragment.get('texture_tiling') is True


def test_csg_fragments_of_an_angled_brush_take_its_tagged_faces_records(main_window):
    host = main_window
    target = _box("target", (0, 0, 0), (256, 64, 256))
    bg.box_to_geometry(target)
    ft.set_transform(target, 'top', shift=(0.6, 0.0), angle=45.0)
    expected = {tag: bf.face_material(target, tag) for tag in bf.FACE_TAGS}
    cutter = box_brush("cutter", (0, 0, 0), (64, 64, 64))
    host.state.brushes.extend([target, cutter])
    host.set_selected_objects([cutter])
    host.perform_subtraction()
    fragments = [b for b in host.state.brushes if b is not cutter
                 and b.get('operation') != 'subtract']
    assert fragments
    for fragment in fragments:
        assert bs_records(fragment) == expected


def test_hollow_shells_take_every_field(main_window, monkeypatch):
    from PyQt5.QtWidgets import QInputDialog
    host = main_window
    outer = _box("outer", (0, 0, 0), (256, 256, 256))
    expected = _records(outer)
    host.state.brushes.append(outer)
    host.set_selected_objects([outer])
    monkeypatch.setattr(QInputDialog, "getInt",
                        staticmethod(lambda *args, **kwargs: (16, True)))
    host.hollow_selected_brush()
    walls = [b for b in host.state.brushes if b.get('operation') != 'subtract']
    assert len(walls) == 6
    for wall in walls:
        assert bs_records(wall) == expected


def bs_records(brush):
    """A box fragment's six records, as drawn."""
    return {tag: bf.face_material(brush, tag) for tag in bf.FACE_TAGS}


def test_the_clip_tool_keeps_records_and_the_cut_face_inherits(main_window):
    state = main_window.state
    brush = _box("clipped", (0, 0, 0), (128, 128, 128))
    before = _records(brush)
    state.brushes.append(brush)
    assert state.clip_brush(brush, [0.3, 1.0, 0.2], 20.0)
    after = _records(brush)
    for key, material in after.items():
        if key.startswith('#'):
            # The cut face inherits the brush's dominant texture and scale.
            assert material.texture in {m.texture for m in before.values()}
        else:
            assert material == before[key], key
    for key in bf.TAG_DICTS.values():
        assert key not in brush, "%s kept as a second copy" % key


def test_undo_and_redo_restore_the_record_exactly(main_window):
    state = main_window.state
    brush = angled_brush("edited", (0, 0, 0), (128, 128, 128),
                         clip_normal=(0.3, 1.0, 0.2))
    state.brushes.append(brush)
    state.selected_objects = [brush]
    cut = next(k for k in bf.face_keys(brush) if k.startswith('#'))
    before = _records(brush)
    state.save_state()
    ft.set_transform(brush, 'top', shift=(0.5, 0.5), scale=(2.0, 4.0), angle=60.0)
    ft.set_transform(brush, cut, natural=True)
    edited = _records(brush)
    assert edited != before
    assert state.undo()
    assert _records(state.brushes[-1]) == before
    assert state.redo()
    assert _records(state.brushes[-1]) == edited
    restored = state.brushes[-1]
    for key in bf.TAG_DICTS.values():
        assert key not in restored


def test_autocaulk_writes_box_records_and_leaves_angled_brushes(main_window):
    host = main_window
    box = _box("box", (0, 0, 0), (64, 64, 64))
    neighbour = _box("neighbour", (64, 0, 0), (64, 64, 64))
    angled = angled_brush("angled", (0, 64, 0), (64, 64, 64))
    angled_before = _records(angled)
    host.state.brushes.extend([box, neighbour, angled])
    host.autocaulk()
    assert bf.face_material(box, 'east').texture == 'nodraw.jpg'
    assert bf.face_material(neighbour, 'west').texture == 'nodraw.jpg'
    assert _records(angled) == angled_before
    assert 'textures' not in angled


def test_the_trigger_toggle_textures_every_face(qt_app):
    import os
    from editor.main_window import MainWindow
    from editor.property_editor import PropertyEditor
    host = MainWindow(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    editor = PropertyEditor(host)
    try:
        for brush in (box_brush("box", (0, 0, 0), (64, 64, 64)),
                      angled_brush("angled", (0, 0, 0), (64, 64, 64))):
            host.state.brushes.append(brush)
            editor.set_object(brush)
            editor.on_trigger_changed(True)
            materials = _records(brush)
            assert {m.texture for m in materials.values()} == {'trigger.jpg'}, brush['name']
            assert len(materials) == len(bf.face_keys(brush))
    finally:
        editor.deleteLater()
        host.unsaved_changes = False
        host.close()
        host.deleteLater()
        qt_app.processEvents()


def test_applying_a_texture_reaches_a_cut_face(main_window, monkeypatch):
    """The asset browser's apply-to-face writes a cut face's plane (its one
    record) and a tagged face's alike, and both draw it."""
    host = main_window
    brush = angled_brush("cut", (0, 0, 0), (128, 128, 128), clip_normal=(0.3, 1.0, 0.2))
    host.state.brushes.append(brush)
    cut = next(k for k in bf.face_keys(brush) if k.startswith('#'))
    monkeypatch.setattr(host.asset_browser, 'get_selected_filepath',
                        lambda: '/assets/textures/Dev/256.jpg')
    for key in (cut, 'top'):
        host.apply_texture_to_specific_face(brush, key)
        assert bf.face_material(brush, key).texture == '256.jpg', key
    index = int(cut[1:])
    assert brush['geometry']['planes'][index]['texture'] == '256.jpg'
    assert 'textures' not in brush
