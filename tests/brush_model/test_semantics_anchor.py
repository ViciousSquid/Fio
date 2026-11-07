"""Stage 0: the brush-semantics oracle states what the engine does.

``tests/helpers/brush_semantics`` is only worth pinning if it describes the
engine rather than an opinion of it. Each reading is checked here against
the code that consumes it, on every brush of every shipped map and on
synthetic brushes covering each material and transform variant:

* **box faces** against the RenderTable columns the instanced box pass
  draws from (texture ids, UV scale/angle/shift, the natural-scale mode
  including brush-wide ``texture_tiling``);
* **convex faces** against the frozen copy of the convex path
  (``tests/helpers/geo_reference``) -- the vertices it hands the GPU, put
  through the model matrix the RenderTable publishes, and the face state of
  every run. ``tests/visual/test_geo_run_parity.py`` proves that frozen copy
  against the GPU's own state, so the chain ends at the GPU;
* **bounds** against the three bound-readers: the RenderTable's culling
  bounds, ``brush_aabb_bounds`` (physics, AI: the same bound rounded to
  float32) and the logic thread's collision rows;
* **pass** (textured or solid) against the RenderTable's class bit;
* **transform** against the RenderTable's rotation column.

Headless: the RenderTable, the frozen reference and the collision rows are
all GL-free.
"""

import copy

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import render_table as rt
from engine.constants import brush_aabb_bounds
from engine.render_table import RenderTable
from tests.helpers import brush_semantics as bs
from tests.helpers import geo_reference as ref
from tests.helpers.paths import REPO_ROOT
from tests.helpers.worlds import angled_brush, box_brush

MAPS = bs.shipped_maps(REPO_ROOT)


def _box_variants():
    """One box per material/transform rule the box pass applies."""
    plain = box_brush("plain", (0, 0, 0), (64, 64, 64))
    plain.pop('textures', None)
    textured = box_brush("textured", (100, 20, -40), (128, 32, 96),
                         textures={'top': 'Dev/128.jpg', 'south': 'caulk.jpg',
                                   'down': 'nodraw.jpg'})
    mapped = box_brush("mapped", (-200, 50, 10), (64, 200, 64),
                       textures={'north': 'Dev/256.jpg'},
                       uv_scale={'north': [2.0, 0.5], 'top': [0.25, 0.25]},
                       uv_angle={'north': 30.0}, uv_shift={'west': [0.5, 0.25]})
    natural = box_brush("natural", (0, 300, 0), (96, 48, 80),
                        uv_natural={'east': True, 'top': True},
                        uv_scale={'east': [3.0, 3.0]})
    tiled = box_brush("tiled", (300, 0, 300), (256, 16, 128), texture_tiling=True,
                      uv_scale={'top': [1.5, 1.5]})
    spun = box_brush("spun", (-300, 0, -300), (64, 96, 32), _rot_angle=40.0,
                     rot_axis=[0.0, 1.0, 0.0])
    tilted = box_brush("tilted", (-300, 0, 300), (64, 96, 32), _rot_angle=15.0)
    return [plain, textured, mapped, natural, tiled, spun, tilted]


def _convex_variants():
    """Angled brushes covering each convex material/transform rule."""
    base = angled_brush("cut", (0, 40, 0), (96, 80, 112),
                        clip_normal=(0.4, 1.0, -0.3), uv_natural={'south': True})
    planes = base['geometry']['planes']
    planes[0]['uv_scale'] = [2.0, 1.0]
    planes[1]['uv_angle'] = 15.0
    planes[2]['uv_shift'] = [0.25, 0.5]
    planes[-1]['texture'] = 'Dev/512.jpg'
    bg.invalidate_geometry_cache(base)
    bare = angled_brush("bare", (200, 60, 0), (100, 120, 90),
                        clip_normal=(-1.0, 0.8, 0.2))
    bare['geometry']['planes'][-1].pop('texture', None)
    bg.invalidate_geometry_cache(bare)
    tiled = angled_brush("tiled", (-200, 60, 0), (100, 120, 90), texture_tiling=True)
    spun = angled_brush("spun", (0, 60, 250), (90, 70, 110),
                        clip_normal=(0.3, 1.0, -0.4))
    spun['_rot_angle'] = 33.0
    spun['rot_axis'] = [0.2, 1.0, 0.1]
    rotated = box_brush("rotated", (0, 60, -250), (90, 70, 110))
    bg.rotate_brush(rotated, 25.0, (0.0, 1.0, 0.0))
    return [base, bare, tiled, spun, rotated]


def _worlds():
    worlds = {name: brushes for name, brushes in MAPS.items()}
    worlds['synthetic'] = _box_variants() + _convex_variants()
    return worlds


WORLDS = _worlds()


def _table(brushes):
    table = RenderTable()
    table.sync(brushes, 1)
    assert table.ids == [b.get('id') for b in brushes]
    return table


def _texture(table, tid):
    return table.texture_names()[tid] if tid >= 0 else None


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_box_faces_are_what_the_box_pass_draws(world):
    brushes = WORLDS[world]
    table = _table(brushes)
    tiling = (table.class_bits & rt.CLASS_TEXTURE_TILING) != 0
    for slot, brush in enumerate(brushes):
        if bs.encoding(brush) != 'box':
            continue
        faces = bs.render_faces(brush)
        assert [tag for tag, _r, _m in faces] == list(rt.CUBE_FACE_KEYS)
        for face, (tag, _ring, material) in enumerate(faces):
            label = "%s %s %s" % (world, brush.get('name'), tag)
            assert _texture(table, table.tex_name_id[slot, face]) == (
                material['texture'] or None), label
            has = bool(table.uv_has_scale[slot, face])
            assert has == (material['uv_scale'] is not None), label
            if has:
                assert tuple(table.uv_scale[slot, face]) == pytest.approx(
                    material['uv_scale']), label
            assert float(table.uv_angle[slot, face]) == pytest.approx(
                material['uv_angle']), label
            assert tuple(table.uv_shift[slot, face]) == pytest.approx(
                material['uv_shift']), label
            # The box pass's rule (instancing._face_uv_scales): a natural
            # flag, or brush-wide tiling on a face with no scale.
            drawn_natural = bool(table.uv_natural[slot, face]) or (
                bool(tiling[slot]) and not has)
            assert drawn_natural == material['natural'], label


def _drawn_faces(brush, table, slot):
    """Per plane index: (world-space vertices, run) as the GPU draws them."""
    mesh = ref.reference_mesh(brush)
    record = table.geometry_records[int(table.geometry_id[slot])]
    models, _normals = rt.model_matrices(table, np.array([slot]))
    model = models[0].reshape(4, 4).T.astype(np.float64)
    out = {}
    for run in mesh.runs:
        local = mesh.vertices[run['first']:run['first'] + run['count'], :3]
        world = (model[:3, :3] @ local.astype(np.float64).T).T + model[:3, 3]
        out[run['plane']] = (world, run)
    return out, record


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_convex_faces_are_what_the_convex_path_draws(world):
    brushes = WORLDS[world]
    table = _table(brushes)
    convex_slots = [s for s, b in enumerate(brushes) if bs.encoding(b) == 'convex']
    if not convex_slots:
        pytest.skip("no convex brushes")
    for slot in convex_slots:
        brush = brushes[slot]
        convex = bg.get_convex(brush)
        drawn, record = _drawn_faces(brush, table, slot)
        # The mesh is anchored to pos/size when the record is made.
        assert list(record.origin) == list(bs.bound(brush)[0])
        assert list(record.scale) == [max(abs(v), 1e-6) for v in bs.bound(brush)[1]]
        faces = bs.render_faces(brush)
        assert len(faces) == len(convex.faces) == len(drawn)
        for face, (tag, ring, material) in zip(convex.faces, faces):
            label = "%s %s plane %d" % (world, brush.get('name'), face['plane'])
            world_points, run = drawn[face['plane']]
            assert run['face'] == tag, label
            assert ref.run_texture(run) == material['texture'], label
            angle, su, sv = ref.run_tex_transform(run)
            assert angle == pytest.approx(np.radians(material['uv_angle'])), label
            assert (su, sv) == pytest.approx(material['uv_shift']), label
            assert run['natural_scale'] == material['natural'], label
            if not run['natural_scale']:
                expected = material['uv_scale'] or (1.0, 1.0)
                assert ref.run_tex_scale(run, None, None) == pytest.approx(expected), label
            # Every drawn vertex is a corner of the polygon the oracle names,
            # and every corner is drawn: the planes are drawn where they are,
            # whatever pos/size say (float32 vertices: 1e-3 of a unit).
            corners = bs.posed(brush, ring)
            gaps = np.abs(world_points[:, None, :] - corners[None, :, :]).max(axis=2)
            tolerance = 1e-3 * max(1.0, float(np.abs(corners).max()) / 1000.0)
            assert gaps.min(axis=1).max() < tolerance, label
            assert gaps.min(axis=0).max() < tolerance, label


@pytest.mark.qt                  # engine.logic_collision imports the editor
@pytest.mark.parametrize("world", sorted(WORLDS))
def test_bounds_are_what_the_collision_rows_use(world):
    from engine.logic_collision import CollisionRows
    brushes = WORLDS[world]
    rows = CollisionRows(brushes, 1)
    for slot, brush in enumerate(brushes):
        centre, size = (np.asarray(v) for v in bs.bound(brush))
        label = "%s %s" % (world, brush.get('name'))
        assert np.array_equal(rows.lo[:, slot], centre - size * 0.5), label
        assert np.array_equal(rows.hi[:, slot], centre + size * 0.5), label


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_bounds_are_what_culling_physics_and_ai_use(world):
    brushes = WORLDS[world]
    table = _table(brushes)
    for slot, brush in enumerate(brushes):
        centre, size = (np.asarray(v) for v in bs.bound(brush))
        label = "%s %s" % (world, brush.get('name'))
        assert np.array_equal(table.center[slot], centre), label
        assert np.array_equal(table.half[slot], size * 0.5), label
        # Physics and AI read the same bound rounded to float32 (computed
        # through GLM, bit for bit as the old glm.vec3 path).
        centre32, half32 = centre.astype(np.float32), size.astype(np.float32) * np.float32(0.5)
        assert brush_aabb_bounds(copy.copy(brush)) == (
            tuple(float(v) for v in centre32 - half32)
            + tuple(float(v) for v in centre32 + half32)), label


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_the_pass_is_the_textured_class_bit(world):
    brushes = WORLDS[world]
    table = _table(brushes)
    for slot, brush in enumerate(brushes):
        textured = bool(table.class_bits[slot] & rt.CLASS_TEXTURED)
        assert bs.draw_pass(brush) == ('textured' if textured else 'solid'), (
            "%s %s" % (world, brush.get('name')))


@pytest.mark.parametrize("world", sorted(WORLDS))
def test_the_transform_is_the_rotation_column(world):
    brushes = WORLDS[world]
    table = _table(brushes)
    for slot, brush in enumerate(brushes):
        _centre, _size, axis, angle = bs.transform(brush)
        label = "%s %s" % (world, brush.get('name'))
        if axis is None:
            assert float(table.rot[slot, 3]) == 0.0, label
        else:
            assert tuple(table.rot[slot]) == pytest.approx(axis + (angle,)), label


def test_the_variants_cover_every_rule():
    """The synthetic world must exercise what it claims to (a guard on the
    fixture, not the engine)."""
    boxes = [m for b in _box_variants() for _t, _r, m in bs.render_faces(b)]
    convex = [m for b in _convex_variants() for _t, _r, m in bs.render_faces(b)]
    for faces in (boxes, convex):
        assert any(m['natural'] for m in faces)
        assert any(m['uv_scale'] is not None for m in faces)
        assert any(m['uv_scale'] is None for m in faces)
        assert any(m['uv_angle'] for m in faces)
        assert any(any(m['uv_shift']) for m in faces)
    assert any(m['texture'] == 'caulk.jpg' for m in boxes)
    assert any(m['texture'] == bs.DEFAULT_TEXTURE for m in convex)
    assert any(bs.transform(b)[1] is not None for b in _convex_variants())
