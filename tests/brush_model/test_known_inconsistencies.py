"""The brush model's known inconsistencies, pinned in both directions.

Every inconsistency the unified brush model is meant to remove appeared here
twice at Stage 0:

* a **characterisation** test asserting what Fio did, exactly; and
* a **target** test asserting the unified contract, marked
  ``xfail(strict=True)`` with the stage expected to deliver it.

So no stage can change one of these behaviours silently: the stage that
fixes one makes its characterisation test fail (update it, and name the
change) and its target test pass unexpectedly (remove the marker). Stage 1
(one face record) has done that for face material: its section now holds the
fixed behaviour as regression tests, and Stage 2 has done the same for the
position targets as each substage delivered them. Shipped maps are not modified here; the
snapshot (``test_semantics_snapshot``) records which maps each behaviour
reaches. See ``docs/brush-model-proposal.md`` for the stages.

What a face renders is read through ``tests/helpers/brush_semantics``, which
``test_semantics_anchor`` ties to the engine and
``tests/visual/test_brush_semantics_gl`` ties to the GPU state.
"""

import copy

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import brush_placement
from engine import render_table as rt
from engine.render_table import RenderTable
from tests.helpers import brush_semantics as bs
from tests.helpers import runtime
from tests.helpers.paths import REPO_ROOT
from tests.helpers.worlds import angled_brush, box_brush

MAPS = bs.shipped_maps(REPO_ROOT)

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
# Face material: one record per face (Stage 1, delivered)
# ---------------------------------------------------------------------------

def test_converting_a_box_to_convex_keeps_its_mapping():
    """Every field of every face moves onto its plane (Stage 0: shift and
    angle were dropped, so the first clip of a mapped box reset them)."""
    box = _mapped_box()
    box['uv_natural'] = {'east': True}
    converted = copy.deepcopy(box)
    bg.box_to_geometry(converted)
    for tag in bs.BOX_FACES:
        assert _face(converted, tag[0]) == _face(box, tag[0]), tag[0]
    assert 'material' not in str(bs.inconsistencies(converted))
    for key in ('textures', 'uv_scale', 'uv_shift', 'uv_angle', 'uv_natural'):
        assert key not in converted, "%s left as a second copy" % key


def test_returning_a_convex_brush_to_a_box_keeps_its_mapping():
    box = _mapped_box()
    round_trip = copy.deepcopy(box)
    bg.box_to_geometry(round_trip)
    bg.drop_geometry(round_trip)
    assert bs.encoding(round_trip) == 'box'
    for tag, _axis, _sign in bs.BOX_FACES:
        assert _face(round_trip, tag) == _face(box, tag), tag


@pytest.mark.qt
def test_an_inspector_edit_of_a_tagged_convex_face_renders_and_reads_back():
    """The Surface Inspector edits the face's one record, so the edit is
    drawn and shown alike (Stage 0: stored in the per-tag dicts, shown by the
    inspector, never drawn)."""
    from editor import face_texture as ft
    brush = _mapped_box()
    bg.box_to_geometry(brush)
    ft.set_transform(brush, 'top', shift=(0.75, 0.0), angle=45.0, scale=(4.0, 4.0))
    drawn = _face(brush, 'top')
    shown = ft.get_transform(brush, 'top')
    assert (drawn['uv_shift'], drawn['uv_angle'], drawn['uv_scale']) == (
        (0.75, 0.0), 45.0, (4.0, 4.0))
    assert (shown['shift'], shown['angle'], shown['scale']) == (
        (0.75, 0.0), 45.0, (4.0, 4.0))
    assert not bs.inconsistencies(brush)


@pytest.mark.qt
def test_an_inspector_edit_of_a_legacy_face_leaves_one_record():
    """A legacy brush's per-tag copy is replaced by the edit, not kept beside
    it: the plane holds the value, the per-tag entry is gone."""
    from editor import face_texture as ft
    brush = angled_brush("legacy", (0, 0, 0), (96, 96, 96))
    for plane in brush['geometry']['planes']:
        plane.pop('uv_scale', None)
    brush['uv_scale'] = {'top': [0.25, 0.25]}            # what _SHOWCASE carries
    from engine import brush_faces
    brush_faces.migrate_legacy(brush)                    # as the editor loads it
    assert _face(brush, 'top')['uv_scale'] == (0.25, 0.25)
    assert ft.get_transform(brush, 'top')['scale'] == (0.25, 0.25)
    ft.set_transform(brush, 'top', scale=(2.0, 2.0))
    assert 'uv_scale' not in brush
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    assert plane['uv_scale'] == [2.0, 2.0]
    assert _face(brush, 'top')['uv_scale'] == (2.0, 2.0)


def test_a_plane_natural_flag_renders():
    """Natural on a cut face is read from its plane (Stage 0: ignored)."""
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    brush['geometry']['planes'][cut]['uv_natural'] = True
    bg.invalidate_geometry_cache(brush)
    convex = bg.get_convex(brush)
    face = next(i for i, f in enumerate(convex.faces) if f['plane'] == cut)
    assert bs.render_faces(brush)[face][2]['natural'] is True
    table = RenderTable()
    table.sync([brush], 1)
    assert table.geometry_records[0].materials[id(convex.faces[face])].natural is True


@pytest.mark.qt
def test_the_editor_writes_natural_for_a_cut_face_and_it_renders():
    from editor import face_texture as ft
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    ft.set_transform(brush, '#%d' % cut, natural=True)
    assert brush['geometry']['planes'][cut].get('uv_natural') is True
    assert ft.get_transform(brush, '#%d' % cut)['natural'] is True
    convex = bg.get_convex(brush)
    face = next(i for i, f in enumerate(convex.faces) if f['plane'] == cut)
    assert bs.render_faces(brush)[face][2]['natural'] is True


def test_texture_tiling_applies_to_convex_faces_as_to_boxes():
    """Brush-wide tiling makes every unscaled face Natural, cut or not
    (Stage 0: convex faces ignored it)."""
    box = box_brush("tiled", (0, 0, 0), (256, 32, 128), texture_tiling=True,
                    uv_scale={'top': [2.0, 2.0]})
    cut = angled_brush("tiled", (0, 0, 0), (256, 32, 128), texture_tiling=True,
                       uv_scale={'top': [2.0, 2.0]})
    box_faces = {t: m['natural'] for t, _r, m in bs.render_faces(box)}
    assert box_faces == {tag: tag != 'top' for tag in box_faces}
    cut_faces = [m for _t, _r, m in bs.render_faces(cut)]
    # The cut face inherits the brush's dominant scale (the clip tool's rule),
    # so only the faces without a scale tile.
    assert all(m['natural'] == (m['uv_scale'] is None) for m in cut_faces)
    assert sum(m['natural'] for m in cut_faces) >= 4


def _plane_textured_only():
    """A convex brush whose only real texture is on a cut face -- what the
    clip tool's texture, or the Surface Inspector on a cut face, makes of an
    untextured box."""
    brush = box_brush("plain", (0, 0, 0), (128, 128, 128))
    brush.pop('textures', None)
    assert bg.clip_brush(brush, [0.3, 1.0, 0.2], 20.0, texture='Dev/128.jpg')
    return brush


def test_a_convex_brush_textured_only_on_its_planes_draws_textured():
    """The pass is decided by the faces' own records (Stage 0: by the
    per-tag dict, so such a brush was drawn in its colour)."""
    brush = _plane_textured_only()
    table = RenderTable()
    table.sync([brush], 1)
    assert table.class_bits[0] & rt.CLASS_TEXTURED
    assert bs.draw_pass(brush) == 'textured'


def test_a_per_tag_texture_never_decides_a_convex_face():
    """Precedence for texture is the plane. The per-tag-only writers of a
    convex brush's texture (autocaulk, the trigger toggle) judged faces by
    the brush's AABB and were never drawn; they stay undrawn."""
    brush = angled_brush("caulked", (0, 0, 0), (96, 96, 96))
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    plane['texture'] = 'Dev/256.jpg'
    brush['textures'] = {'top': 'nodraw.jpg'}
    bg.invalidate_geometry_cache(brush)
    assert _face(brush, 'top')['texture'] == 'Dev/256.jpg'
    assert bs.inconsistencies(brush)['discarded_tag_value'] == ['top.texture']


def test_every_loaded_shipped_convex_face_has_one_material_record():
    """After the load-time migration (as ``EditorState`` loads a legacy map),
    no tagged convex face keeps a per-tag copy -- and every face draws what
    the precedence rule (restated in the helper) says."""
    from engine import brush_faces
    for name, brushes in MAPS.items():
        for original in brushes:
            if bs.encoding(original) != 'convex':
                continue
            brush = dict(original)
            before = bs.render_faces(bs.migrated(original))
            brush_faces.migrate_legacy(brush)
            tags = {p.get('face') for p in brush['geometry']['planes'] if p.get('face')}
            for key, _field in bs.TAG_FIELDS:
                assert not tags & set(brush.get(key) or {}), (name, key)
            assert bs.render_faces(brush) == before, name


# ---------------------------------------------------------------------------
# Position: geometry, bound and transform at once (Stage 2)
# ---------------------------------------------------------------------------

def test_shipped_convex_bounds_drift_but_contain_what_is_drawn():
    """Seven shipped convex brushes *store* ``pos``/``size`` that are not
    their planes' AABB bit for bit: four by float noise, one by 4.85e-5 (its
    bound is that much *smaller* than the geometry) and two by 128 units
    (bounds that are larger). Within 1e-4 of a unit every stored bound
    contains the drawn geometry. Since Stage 2a nothing reads that copy for
    a bound (``brush_placement`` derives it from the planes) and loading
    re-derives it; the files themselves are not modified."""
    drifted = 0
    for name, brushes in MAPS.items():
        for brush in brushes:
            found = bs.inconsistencies(brush)
            if 'bound_drift' not in found:
                continue
            drifted += 1
            centre, size = (np.asarray(v) for v in bs.stored_bound(brush))
            g_centre, g_size = (np.asarray(v) for v in bs.geometry_bound(brush))
            assert (g_centre - g_size / 2 >= centre - size / 2 - 1e-4).all(), name
            assert (g_centre + g_size / 2 <= centre + size / 2 + 1e-4).all(), name
    assert drifted == 7     # 3 above 1e-6 (pinned by the snapshot), 4 noise


def test_shipped_convex_bounds_are_derived_from_the_planes():
    """Stage 2a: every shipped convex brush's derived bounds -- what culling,
    collision rows, the physics grid, Big World and picking read -- are its
    drawn geometry's AABB, the 128-unit brushes included; and loading
    re-derives the dict's ``pos``/``size`` copy from the planes."""
    checked = 0
    for name, brushes in MAPS.items():
        for raw in brushes:
            if not bg.brush_has_geometry(raw):
                continue
            brush = copy.deepcopy(raw)
            g_centre, g_size = (np.asarray(v) for v in bs.geometry_bound(brush))
            lo = np.asarray(brush_placement.world_bounds(brush)[:3])
            hi = np.asarray(brush_placement.world_bounds(brush)[3:])
            assert np.allclose(lo, g_centre - g_size / 2, atol=1e-9), (name, brush['id'])
            assert np.allclose(hi, g_centre + g_size / 2, atol=1e-9), (name, brush['id'])
            brush_placement.normalise_loaded(brush)
            assert 'bound_drift' not in bs.inconsistencies(brush), (name, brush['id'])
            checked += 1
    assert checked >= 7


def test_a_size_only_write_does_not_stretch_a_convex_brush():
    """Stage 2a: a convex mesh is anchored to its own planes' bounds, and the
    row's transform is those bounds too, so a ``size``-only write (the
    console's ``setprop``, a plugin) changes nothing drawn: the record is
    kept and the planes are drawn where they are, before and after a
    reload. (Before Stage 2a the anchor was ``pos``/``size`` and the write
    stretched the brush until the map was reloaded.)"""
    brush = angled_brush("anchored", (40, 30, -20), (96, 80, 112),
                         clip_normal=(0.4, 1.0, -0.3))
    planes = np.asarray(bg.get_convex(brush).verts, dtype=np.float64)
    live = RenderTable()
    live.sync([brush], 1)
    record = live.geometry_records[0]
    brush['size'] = [brush['size'][0] * 1.5] + list(brush['size'][1:])
    live.sync([brush], 2, dirty_objects={id(brush)})
    assert live.geometry_records[0] is record
    assert np.allclose(_drawn_points(live, 0), planes, atol=1e-3)
    fresh = RenderTable()
    fresh.sync([copy.deepcopy({k: v for k, v in brush.items()
                               if k not in bg.GEO_RUNTIME_KEYS})], 1)
    assert np.allclose(_drawn_points(fresh, 0), planes, atol=1e-3)


def test_what_is_drawn_does_not_depend_on_edit_history():
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
    """A mover's runtime transform (Stage 2b: owned by a runtime source,
    never written into the brush) moves the drawn convex brush; its
    collision triangles, baked in world space at play start, do not."""
    brush = _convex_mover()
    triangles = copy.deepcopy(brush['_mesh_triangles'])
    table = RenderTable()
    table.sync([brush], 1)
    before = _drawn_points(table, 0)
    runtime.drive(brush, offset=(0.0, 96.0, 0.0))
    table.refresh_transforms([brush], np.array([0]))
    after = _drawn_points(table, 0)
    assert np.allclose(after - before, [0.0, 96.0, 0.0], atol=1e-3)
    assert brush['_mesh_triangles'] == triangles


@pytest.mark.xfail(strict=True, reason=STAGE_2)
def test_target_a_convex_movers_collision_follows_its_transform():
    brush = _convex_mover()
    lowest = min(v[1] for tri in brush['_mesh_triangles'] for v in tri)
    runtime.drive(brush, offset=(0.0, 96.0, 0.0))
    moved = min(v[1] for tri in brush['_mesh_triangles'] for v in tri)
    assert moved == pytest.approx(lowest + 96.0)


@pytest.mark.qt
def test_a_saved_map_carries_no_play_state():
    """Stage 2b: play writes nothing into the brush dict, and the serialiser
    drops the keys older builds wrote (``original_pos``, ``_rot_angle``,
    ``rotation_yaw`` -- how seven shipped maps came to carry them)."""
    from editor.editor_state import EditorState
    state = EditorState()
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64), is_mover=True)
    brush.update(original_pos=[0.0, 0.0, 0.0], _rot_angle=12.0, rotation_yaw=12.0)
    state.brushes = [brush]
    saved = state._serialize_brushes()[0]
    assert not {k for k in bs.RUNTIME_KEYS if k in saved}
