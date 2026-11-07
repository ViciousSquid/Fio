"""Stage 0: the brush model's known inconsistencies, pinned in both directions.

Every inconsistency the unified brush model is meant to remove appears here
twice:

* a **characterisation** test asserting what Fio does today, exactly; and
* a **target** test asserting the unified contract, marked
  ``xfail(strict=True)`` with the stage expected to deliver it.

So no stage can change one of these behaviours silently: the stage that
fixes one makes its characterisation test fail (update it, and name the
change) and its target test pass unexpectedly (remove the marker). Shipped
maps are not modified here; the snapshot (``test_semantics_snapshot``)
records which maps each behaviour reaches. See
``docs/brush-model-proposal.md`` for the stages.

What a face renders is read through ``tests/helpers/brush_semantics``, which
``test_semantics_anchor`` ties to the engine and
``tests/visual/test_brush_semantics_gl`` ties to the GPU state.
"""

import copy

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import render_table as rt
from engine.render_table import RenderTable
from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT
from tests.helpers.worlds import angled_brush, box_brush

MAPS = bs.shipped_maps(REPO_ROOT)

STAGE_1 = "Stage 1 (one face record) delivers this"
STAGE_2 = "Stage 2 (unambiguous position) delivers this"


def _face(brush, tag):
    return next(m for t, _r, m in bs.render_faces(brush) if t == tag)


def _mapped_box():
    return box_brush("mapped", (0, 0, 0), (128, 64, 256),
                     textures={'top': 'Dev/128.jpg'},
                     uv_shift={'top': [0.25, 0.5]}, uv_angle={'top': 30.0},
                     uv_scale={'top': [2.0, 2.0]})


def _drawn_points(table, slot):
    """Every convex vertex where the GPU draws it: the record's anchored
    mesh through the row's published model matrix."""
    record = table.geometry_records[int(table.geometry_id[slot])]
    local = (record.convex.verts - record.origin) / record.scale
    models, _ = rt.model_matrices(table, np.array([slot]))
    model = models[0].reshape(4, 4).T.astype(np.float64)
    return (model[:3, :3] @ local.astype(np.float32).astype(np.float64).T).T + model[:3, 3]


# ---------------------------------------------------------------------------
# Face material: two homes (Stage 1)
# ---------------------------------------------------------------------------

def test_converting_a_box_to_convex_drops_its_shift_and_angle():
    """``box_to_geometry`` carries textures and scales onto the planes, not
    shifts or angles, and the convex path reads those from the plane only.
    The first clip, vertex drag or rotation of a mapped box therefore resets
    the mapping on screen while the per-tag dicts still hold it."""
    box = _mapped_box()
    before = _face(box, 'top')
    converted = copy.deepcopy(box)
    bg.box_to_geometry(converted)
    after = _face(converted, 'top')
    assert (before['uv_shift'], before['uv_angle']) == ((0.25, 0.5), 30.0)
    assert (after['uv_shift'], after['uv_angle']) == ((0.0, 0.0), 0.0)
    assert (after['texture'], after['uv_scale']) == (before['texture'], before['uv_scale'])
    assert sorted(bs.inconsistencies(converted)['material_mismatch']) == [
        'top.uv_angle', 'top.uv_shift']


@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_converting_a_box_to_convex_keeps_its_mapping():
    box = _mapped_box()
    converted = copy.deepcopy(box)
    bg.box_to_geometry(converted)
    assert _face(converted, 'top') == _face(box, 'top')


@pytest.mark.qt
def test_an_inspector_edit_of_a_tagged_convex_face_is_stored_where_it_does_not_render():
    """The Surface Inspector writes a tagged face's shift, scale and angle to
    the per-tag dicts and reads them back from there; the convex path draws
    the plane's. The inspector shows the edit, the viewport does not. (Four
    convex brushes in ``_SHOWCASE`` carry per-tag scales their planes lack.)"""
    from editor import face_texture as ft
    brush = _mapped_box()
    bg.box_to_geometry(brush)
    drawn = _face(brush, 'top')
    ft.set_transform(brush, 'top', shift=(0.75, 0.0), angle=45.0, scale=(4.0, 4.0))
    shown = ft.get_transform(brush, 'top')
    assert (shown['shift'], shown['angle'], shown['scale']) == ((0.75, 0.0), 45.0, (4.0, 4.0))
    assert _face(brush, 'top') == drawn
    assert sorted(bs.inconsistencies(brush)['material_mismatch']) == [
        'top.uv_angle', 'top.uv_scale', 'top.uv_shift']


@pytest.mark.qt
@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_an_inspector_edit_of_a_tagged_convex_face_renders():
    from editor import face_texture as ft
    brush = _mapped_box()
    bg.box_to_geometry(brush)
    ft.set_transform(brush, 'top', shift=(0.75, 0.0), angle=45.0, scale=(4.0, 4.0))
    drawn = _face(brush, 'top')
    assert (drawn['uv_shift'], drawn['uv_angle'], drawn['uv_scale']) == (
        (0.75, 0.0), 45.0, (4.0, 4.0))


def test_a_retexture_writes_both_homes_and_they_agree():
    """Texture is the one field the editor keeps in step: a tagged convex
    face's texture is written to the plane and the per-tag dict alike."""
    brush = angled_brush("re", (0, 0, 0), (96, 96, 96))
    tag = next(p['face'] for p in brush['geometry']['planes'] if p.get('face'))
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == tag)
    plane['texture'] = 'Dev/256.jpg'
    brush.setdefault('textures', {})[tag] = 'Dev/256.jpg'
    bg.invalidate_geometry_cache(brush)
    assert _face(brush, tag)['texture'] == 'Dev/256.jpg'
    assert 'material_mismatch' not in bs.inconsistencies(brush)


def test_a_plane_natural_flag_does_not_render():
    """The renderer resolves a convex face's natural-scale mode from the
    brush's per-tag flags only (it hands the face dict, which has no
    ``uv_natural``, as the plane). The editor writes the flag on the plane
    for a cut face, so Natural on a cut face never shows."""
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    brush['geometry']['planes'][cut]['uv_natural'] = True
    bg.invalidate_geometry_cache(brush)
    convex = bg.get_convex(brush)
    face = next(i for i, f in enumerate(convex.faces) if f['plane'] == cut)
    assert bs.render_faces(brush)[face][2]['natural'] is False
    assert bs.inconsistencies(brush)['ignored_plane_natural'] == [cut]
    table = RenderTable()
    table.sync([brush], 1)
    record = table.geometry_records[0]
    assert record.natural_scale[id(convex.faces[face])] is False


@pytest.mark.qt
def test_the_editor_writes_natural_for_a_cut_face_on_the_plane():
    from editor import face_texture as ft
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    ft.set_transform(brush, '#%d' % cut, natural=True)
    assert brush['geometry']['planes'][cut].get('uv_natural') is True
    assert ft.get_transform(brush, '#%d' % cut)['natural'] is True
    assert bs.inconsistencies(brush)['ignored_plane_natural'] == [cut]


@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_a_plane_natural_flag_renders():
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    brush['geometry']['planes'][cut]['uv_natural'] = True
    bg.invalidate_geometry_cache(brush)
    convex = bg.get_convex(brush)
    face = next(i for i, f in enumerate(convex.faces) if f['plane'] == cut)
    assert bs.render_faces(brush)[face][2]['natural'] is True


def test_texture_tiling_does_not_reach_convex_faces():
    """Brush-wide ``texture_tiling`` makes every unscaled box face natural;
    the convex path ignores it, so the same brush draws differently once it
    is cut."""
    box = box_brush("tiled", (0, 0, 0), (256, 32, 128), texture_tiling=True)
    assert all(m['natural'] for _t, _r, m in bs.render_faces(box))
    cut = angled_brush("tiled", (0, 0, 0), (256, 32, 128), texture_tiling=True)
    assert not any(m['natural'] for _t, _r, m in bs.render_faces(cut))
    assert bs.inconsistencies(cut)['ignored_texture_tiling']


@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_texture_tiling_applies_to_convex_faces():
    cut = angled_brush("tiled", (0, 0, 0), (256, 32, 128), texture_tiling=True)
    assert all(m['natural'] for _t, _r, m in bs.render_faces(cut))


def _plane_textured_only():
    """A convex brush whose only real texture is on a cut face -- what the
    clip tool's texture, or the Surface Inspector on a cut face, makes of an
    untextured box."""
    brush = box_brush("plain", (0, 0, 0), (128, 128, 128))
    brush.pop('textures', None)
    assert bg.clip_brush(brush, [0.3, 1.0, 0.2], 20.0, texture='Dev/128.jpg')
    return brush


def test_a_convex_brush_textured_only_on_its_planes_is_drawn_untextured():
    """Whether a brush goes to the textured pass is decided from its per-tag
    ``textures`` dict, for convex brushes too: a texture held only by a
    plane does not count, and the solid pass draws every face in the brush
    colour. No shipped map has such a brush; the editor can make one."""
    brush = _plane_textured_only()
    assert 'Dev/128.jpg' in [m['texture'] for _t, _r, m in bs.render_faces(brush)]
    table = RenderTable()
    table.sync([brush], 1)
    assert not table.class_bits[0] & rt.CLASS_TEXTURED
    assert bs.draw_pass(brush) == 'solid'
    assert bs.inconsistencies(brush)['untextured_planes'] == [
        len(brush['geometry']['planes']) - 1]


@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_a_convex_brush_textured_on_its_planes_draws_textured():
    assert bs.draw_pass(_plane_textured_only()) == 'textured'


@pytest.mark.xfail(strict=True, reason=STAGE_1)
def test_target_a_shipped_convex_face_has_one_material_record():
    """No tagged convex face in a shipped map also keeps a per-tag copy of
    its material (the copy the Surface Inspector shows today)."""
    doubled = []
    for name, brushes in MAPS.items():
        for brush in brushes:
            if bs.encoding(brush) != 'convex':
                continue
            tags = {p.get('face') for p in brush['geometry']['planes'] if p.get('face')}
            for key, _field in bs.TAG_FIELDS:
                if tags & set(brush.get(key) or {}):
                    doubled.append((name, brush.get('id'), key))
    assert not doubled


# ---------------------------------------------------------------------------
# Position: geometry, bound and transform at once (Stage 2)
# ---------------------------------------------------------------------------

def test_shipped_convex_bounds_drift_but_contain_what_is_drawn():
    """Seven shipped convex brushes have ``pos``/``size`` that are not their
    planes' AABB bit for bit: four by float noise, one by 4.85e-5 (its bound
    is that much *smaller* than the geometry) and two by 128 units (bounds
    that are larger). Within 1e-4 of a unit every bound contains the drawn
    geometry, so culling, collision broad-phase and picking -- which read
    the bound -- are conservative. A derived bound will be tighter: that is
    a deliberate change of those maps' culling and broad-phase, not of their
    pixels."""
    drifted = 0
    for name, brushes in MAPS.items():
        for brush in brushes:
            found = bs.inconsistencies(brush)
            if 'bound_drift' not in found:
                continue
            drifted += 1
            centre, size = (np.asarray(v) for v in bs.bound(brush))
            g_centre, g_size = (np.asarray(v) for v in bs.geometry_bound(brush))
            assert (g_centre - g_size / 2 >= centre - size / 2 - 1e-4).all(), name
            assert (g_centre + g_size / 2 <= centre + size / 2 + 1e-4).all(), name
    assert drifted == 7     # 3 above 1e-6 (pinned by the snapshot), 4 noise


@pytest.mark.xfail(strict=True, reason=STAGE_2)
def test_target_shipped_convex_bounds_are_derived_from_the_planes():
    drifted = [(name, b.get('id')) for name, brushes in MAPS.items()
               for b in brushes
               if bs.inconsistencies(b).get('bound_drift', 0.0) > bs.DRIFT_NOISE]
    assert not drifted


def test_size_of_an_existing_convex_brush_is_a_transform():
    """A convex mesh is anchored to ``pos``/``size`` when its record is made
    and kept while its planes are unchanged, so a ``size``-only change (the
    console's ``setprop``, a plugin) stretches the drawn brush -- until the
    map is reloaded, when the planes are drawn where they are. What is drawn
    depends on the brush's history, not only on its dict."""
    brush = angled_brush("anchored", (40, 30, -20), (96, 80, 112),
                         clip_normal=(0.4, 1.0, -0.3))
    planes = np.asarray(bg.get_convex(brush).verts, dtype=np.float64)
    live = RenderTable()
    live.sync([brush], 1)
    record = live.geometry_records[0]
    brush['size'] = [brush['size'][0] * 1.5] + list(brush['size'][1:])
    live.sync([brush], 2, dirty_objects={id(brush)})
    assert live.geometry_records[0] is record
    stretched = _drawn_points(live, 0)
    fresh = RenderTable()
    fresh.sync([copy.deepcopy({k: v for k, v in brush.items()
                               if k not in bg.GEO_RUNTIME_KEYS})], 1)
    reloaded = _drawn_points(fresh, 0)
    assert np.allclose(reloaded, planes, atol=1e-3)
    assert not np.allclose(stretched, planes, atol=1.0)


@pytest.mark.xfail(strict=True, reason=STAGE_2)
def test_target_what_is_drawn_does_not_depend_on_edit_history():
    brush = angled_brush("anchored", (40, 30, -20), (96, 80, 112),
                         clip_normal=(0.4, 1.0, -0.3))
    live = RenderTable()
    live.sync([brush], 1)
    brush['size'] = [brush['size'][0] * 1.5] + list(brush['size'][1:])
    live.sync([brush], 2, dirty_objects={id(brush)})
    fresh = RenderTable()
    fresh.sync([copy.deepcopy({k: v for k, v in brush.items()
                               if k not in bg.GEO_RUNTIME_KEYS})], 1)
    assert np.allclose(_drawn_points(live, 0), _drawn_points(fresh, 0), atol=1e-3)


def _convex_mover():
    brush = angled_brush("lift", (0, 64, 0), (128, 32, 128),
                         clip_normal=(0.2, 1.0, 0.0), is_mover=True)
    assert bg.build_collision_mesh(brush)
    return brush


def test_a_convex_movers_collision_mesh_stays_where_play_started():
    """Movers move a brush by writing ``pos``. The drawn convex brush follows
    (``pos`` is its transform); its collision triangles, baked in world space
    at play start, do not."""
    brush = _convex_mover()
    triangles = copy.deepcopy(brush['_mesh_triangles'])
    table = RenderTable()
    table.sync([brush], 1)
    before = _drawn_points(table, 0)
    brush['original_pos'] = list(brush['pos'])
    brush['pos'] = [brush['pos'][0], brush['pos'][1] + 96.0, brush['pos'][2]]
    table.refresh_transforms([brush], np.array([0]))
    after = _drawn_points(table, 0)
    assert np.allclose(after - before, [0.0, 96.0, 0.0], atol=1e-3)
    assert brush['_mesh_triangles'] == triangles


@pytest.mark.xfail(strict=True, reason=STAGE_2)
def test_target_a_convex_movers_collision_follows_its_transform():
    brush = _convex_mover()
    lowest = min(v[1] for tri in brush['_mesh_triangles'] for v in tri)
    brush['original_pos'] = list(brush['pos'])
    brush['pos'] = [brush['pos'][0], brush['pos'][1] + 96.0, brush['pos'][2]]
    moved = min(v[1] for tri in brush['_mesh_triangles'] for v in tri)
    assert moved == pytest.approx(lowest + 96.0)


@pytest.mark.qt
def test_a_saved_map_keeps_plays_keys_in_the_authored_dict():
    """Play writes ``original_pos`` (and a spinner's ``_rot_angle`` and
    ``rotation_yaw``) into the brush dict; the map serialiser keeps them --
    which is how seven shipped maps came to carry them."""
    from editor.editor_state import EditorState
    state = EditorState()
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64), is_mover=True)
    brush.update(original_pos=[0.0, 0.0, 0.0], _rot_angle=12.0, rotation_yaw=12.0)
    state.brushes = [brush]
    saved = state._serialize_brushes()[0]
    assert {k for k in bs.RUNTIME_KEYS if k in saved} == set(bs.RUNTIME_KEYS)


@pytest.mark.qt
@pytest.mark.xfail(strict=True, reason=STAGE_2)
def test_target_a_saved_map_carries_no_play_state():
    from editor.editor_state import EditorState
    state = EditorState()
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64), is_mover=True)
    brush.update(original_pos=[0.0, 0.0, 0.0], _rot_angle=12.0, rotation_yaw=12.0)
    state.brushes = [brush]
    saved = state._serialize_brushes()[0]
    assert not {k for k in bs.RUNTIME_KEYS if k in saved}
