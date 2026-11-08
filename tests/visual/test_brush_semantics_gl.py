"""Visual tier: what the GPU draws angled faces with is their one record.

Stage 1 of the unified brush model gives every face one material record
(``engine.brush_faces``). These tests read the GPU's own state for every
drawn triangle through the draw oracle -- the bound texture and the UV
scale, angle and shift the program will use -- and check it against that
record, for the cases Stage 0 found broken:

* Surface Inspector shift, scale and rotation edits change what the GPU
  draws (tagged and cut faces of angled brushes; box faces by pixels);
* converting a box to convex keeps every face's mapping;
* a brush textured only on its planes is drawn by the textured pass;
* Natural and brush-wide tiling apply to angled faces;
* a legacy brush (per-tag copies beside the planes, as ``_SHOWCASE``
  carries) draws by the precedence rule;
* saving and reloading draws the same triangles with the same UV state.

So "the inspector's edit renders" is a statement about GPU state, not about
a helper's reading of the dicts.
"""

import copy
import json

import numpy as np
import pytest

from tests.helpers import brush_semantics as bs
from tests.helpers import gl as glh
from tests.visual.test_geo_run_parity import (  # noqa: F401 (fixtures)
    _production, _setup, context, renderer)

pytestmark = [pytest.mark.gl, pytest.mark.qt]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _recording(renderer):
    """Wrap the texture loader so a bound GL texture names its file."""
    names = {}
    load = renderer.load_texture_callback

    def recording(name, subfolder):
        tex = load(name, subfolder)
        names[int(tex)] = name
        return tex

    renderer.load_texture_callback = recording
    return names


def _unique_textures(brush, label):
    """A texture of its own for every face, so a triangle names its face."""
    from engine import brush_faces as bf
    for key in bf.face_keys(brush):
        bf.set_face_material(brush, key, texture='probe/%s/%s.png' % (label, key))
    return brush


def _drawn_state(renderer, context, brushes, names, batching='elements'):
    """``{texture name: set of (scale, angle, shift)}`` over every triangle
    the textured pass draws for *brushes*."""
    from tests.helpers.draw_oracle import (DrawCapture, _f32,
                                           _triangles_of_capture, convex_programs)
    renderer.geo_batching = batching
    config, table, slots, camera = _setup(renderer, context, brushes, [], False)
    with DrawCapture(program=convex_programs(renderer),
                     skip_buffers=[renderer._cube_vbo]) as capture:
        _production(renderer, context, config, table, slots, camera, None)
    out = {}
    for draw in capture.draws:
        for (_vertices, texture, _cull, scale, angle, shift), _m, _n in \
                _triangles_of_capture(draw):
            state = (tuple(np.round(_f32(scale), 5)), round(float(_f32(angle)[0]), 5),
                     tuple(np.round(_f32(shift), 5)))
            out.setdefault(names.get(texture, texture), set()).add(state)
    return out


def _expected_state(renderer, brush, key, material):
    """The (scale, angle, shift) the oracle says a face is drawn with."""
    from editor import face_texture as ft
    from engine import brush_geometry as bg
    if material['natural']:
        width, height = ft.face_extent(brush, key)
        scale = bg.natural_repeats(width, height,
                                   renderer._texture_pixel_size(material['texture']))
    else:
        scale = material['uv_scale'] or (1.0, 1.0)
    return (tuple(np.round(np.float32(scale), 5)),
            round(float(np.float32(np.radians(material['uv_angle']))), 5),
            tuple(np.round(np.float32(material['uv_shift']), 5)))


def _check(renderer, brushes, drawn):
    """Every face of every brush is drawn, exactly with its record's state."""
    from engine import brush_faces as bf
    checked = 0
    for brush in brushes:
        keys = bf.face_keys(brush)
        for key, (_tag, _ring, material) in zip(keys, bs.render_faces(brush)):
            assert drawn.get(material['texture']) == {
                _expected_state(renderer, brush, key, material)}, (brush['name'], key)
            checked += 1
    return checked


# ---------------------------------------------------------------------------
# Brushes
# ---------------------------------------------------------------------------

def _angled(name, pos, **props):
    from tests.helpers.worlds import angled_brush
    return angled_brush(name, pos, (96, 80, 96), clip_normal=(0.3, 1.0, -0.2), **props)


def _cut_key(brush):
    return '#%d' % next(i for i, p in enumerate(brush['geometry']['planes'])
                        if not p.get('face'))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ("shift", "scale", "angle"))
@pytest.mark.parametrize("batching", ("elements", "off"))
def test_a_surface_inspector_edit_changes_the_uvs_the_gpu_draws(
        renderer, context, field, batching):
    """Tagged and cut face alike: the edited value reaches the GPU, and no
    other face's state moves."""
    from editor import face_texture as ft
    names = _recording(renderer)
    brush = _unique_textures(_angled("inspected", (0, 40, 0)), 'inspected')
    other = _unique_textures(_angled("other", (180, 40, 0)), 'other')
    before = _drawn_state(renderer, context, [brush, other], names, batching)
    edits = {'shift': {'shift': (0.75, 0.25)}, 'scale': {'scale': (4.0, 0.5)},
             'angle': {'angle': 45.0}}[field]
    for key in ('top', _cut_key(brush)):
        ft.set_transform(brush, key, **edits)
    after = _drawn_state(renderer, context, [brush, other], names, batching)
    edited = {'probe/inspected/top.png',
              'probe/inspected/%s.png' % _cut_key(brush)}
    for texture in before:
        if texture in edited:
            assert after[texture] != before[texture], texture
        else:
            assert after[texture] == before[texture], texture
    assert _check(renderer, [brush, other], after) == (
        len(bs.render_faces(brush)) + len(bs.render_faces(other)))
    shown = ft.get_transform(brush, 'top')
    assert {'shift': shown['shift'], 'scale': shown['scale'],
            'angle': shown['angle']}[field] == list(edits.values())[0]


@pytest.mark.parametrize("batching", ("elements", "off"))
def test_a_surface_inspector_natural_toggle_reaches_the_gpu(renderer, context, batching):
    """Natural on, then off, on a tagged and a cut face: the GPU draws the
    constant-texel scale while it is on and the authored scale once it is
    off -- the flag is enabled and disabled on its own, and a stale legacy
    copy (as an old map carried) does not bring it back."""
    from editor import face_texture as ft
    names = _recording(renderer)
    brush = _unique_textures(_angled("natural", (0, 40, 0)), 'natural')
    keys = ('top', _cut_key(brush))
    for key in keys:
        ft.set_transform(brush, key, scale=(2.0, 3.0))
    off = _drawn_state(renderer, context, [brush], names, batching)
    for key in keys:
        ft.set_transform(brush, key, natural=True)
    on = _drawn_state(renderer, context, [brush], names, batching)
    for key in keys:
        texture = 'probe/natural/%s.png' % key
        (scale, _a, _s), = on[texture]
        assert scale != (2.0, 3.0) and on[texture] != off[texture], key
    assert _check(renderer, [brush], on) > 0
    for key in keys:
        ft.set_transform(brush, key, natural=False)
    brush['uv_natural'] = {'top': True}             # an obsolete legacy copy
    again = _drawn_state(renderer, context, [brush], names, batching)
    assert again == off
    assert not ft.get_transform(brush, 'top')['natural']


def test_a_surface_inspector_edit_of_a_box_face_changes_its_pixels(renderer, context):
    """Box faces draw through the instanced cube path; the edit shows."""
    from editor import face_texture as ft
    from tests.helpers.worlds import box_brush
    box = box_brush("box", (0, 40, 0), (160, 80, 160),
                    textures={tag: 'Dev/128.jpg' for tag in
                              ('south', 'north', 'west', 'east', 'down', 'top')})

    def frame():
        config = glh.render_config(all_brushes=[box], all_things=[],
                                   play_mode=False, shadows_enabled=False)
        projection, view, eye = glh.camera_matrices(
            aspect=1.0, eye=(0.0, 320.0, 160.0), target=(0.0, 40.0, 0.0))
        context.bind()
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config['all_brush_slots'])
        return context.read_pixels()

    base = frame()
    for edit in ({'shift': (0.5, 0.25)}, {'scale': (3.0, 3.0)}, {'angle': 30.0}):
        ft.set_transform(box, 'top', **edit)
        image = frame()
        assert (image != base).any(), edit
        base = image


def test_converting_a_box_to_convex_keeps_the_mapping_the_gpu_draws(renderer, context):
    """Each face of the converted brush draws with the box's own per-tag
    material, as the box pass read it from the RenderTable."""
    from engine import brush_geometry as bg
    from engine.render_table import RenderTable, CUBE_FACE_KEYS
    from tests.helpers.worlds import box_brush
    names = _recording(renderer)
    box = box_brush("mapped", (0, 40, 0), (128, 64, 96),
                    textures={tag: 'probe/box/%s.png' % tag for tag in CUBE_FACE_KEYS},
                    uv_scale={'top': [2.0, 0.5], 'north': [3.0, 3.0]},
                    uv_shift={'top': [0.25, 0.5], 'west': [0.1, 0.0]},
                    uv_angle={'top': 30.0, 'east': 90.0},
                    uv_natural={'south': True})
    table = RenderTable()
    table.sync([box], 1)
    box_state = {}
    for face, tag in enumerate(CUBE_FACE_KEYS):
        has = bool(table.uv_has_scale[0, face])
        box_state[tag] = {
            'texture': table.texture_names()[table.tex_name_id[0, face]],
            'uv_scale': tuple(float(v) for v in table.uv_scale[0, face]) if has else None,
            'uv_angle': float(table.uv_angle[0, face]),
            'uv_shift': tuple(float(v) for v in table.uv_shift[0, face]),
            'natural': bool(table.uv_natural[0, face]),
        }
    converted = copy.deepcopy(box)
    bg.box_to_geometry(converted)
    drawn = _drawn_state(renderer, context, [converted], names)
    for tag, material in box_state.items():
        assert drawn[material['texture']] == {
            _expected_state(renderer, converted, tag, material)}, tag


def test_a_brush_textured_only_on_its_planes_is_drawn_by_the_textured_pass(
        renderer, context):
    """Stage 0: such a brush went to the solid pass and was drawn in its
    colour. Its faces' own records decide now."""
    from engine import brush_geometry as bg
    from tests.helpers.draw_oracle import DrawCapture, convex_programs
    from tests.helpers.worlds import box_brush
    names = _recording(renderer)
    brush = box_brush("plain", (0, 40, 0), (128, 128, 128))
    brush.pop('textures', None)
    assert bg.clip_brush(brush, [0.3, 1.0, 0.2], 60.0, texture='Dev/128.jpg')
    assert bs.draw_pass(brush) == 'textured'
    config = glh.render_config(all_brushes=[brush], all_things=[],
                               play_mode=False, shadows_enabled=False)
    projection, view, eye = glh.camera_matrices(
        aspect=1.0, eye=(0.0, 300.0, 400.0), target=(0.0, 40.0, 0.0))
    context.bind()
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=config['all_brush_slots'])
    lit = [renderer.shaders.get(name) for name in ('lit', 'lit_brush_instanced')]
    captures = []
    for programs in (convex_programs(renderer), lit):
        with DrawCapture(program=programs) as capture:
            renderer.render_scene(projection, view, eye, None, config,
                                  brush_slots=config['all_brush_slots'])
        captures.append(capture)
    textured, solid = captures
    assert textured.calls > 0 and solid.calls == 0
    assert 'Dev/128.jpg' in {names.get(d['texture']) for d in textured.draws}


def test_natural_and_tiling_apply_to_angled_faces(renderer, context):
    """A Natural cut face and a tiled brush's unscaled faces draw at a
    constant texel size: extent over texture size, not 1.0."""
    from editor import face_texture as ft
    from engine import brush_faces as bf
    names = _recording(renderer)
    natural = _unique_textures(_angled("natural", (0, 40, 0)), 'natural')
    ft.set_transform(natural, _cut_key(natural), natural=True)
    ft.set_transform(natural, 'north', natural=True)
    tiled = _unique_textures(_angled("tiled", (180, 40, 0), texture_tiling=True), 'tiled')
    ft.set_transform(tiled, 'top', scale=(2.0, 2.0))
    drawn = _drawn_state(renderer, context, [natural, tiled], names)
    assert _check(renderer, [natural, tiled], drawn) > 0
    for brush, keys in ((natural, (_cut_key(natural), 'north')),
                        (tiled, [k for k in ('south', 'west', 'down') if k in bf.face_keys(tiled)])):
        for key in keys:
            (scale, _a, _s), = drawn['probe/%s/%s.png' % (brush['name'], key)]
            assert scale != (1.0, 1.0), (brush['name'], key)
    (scale, _a, _s), = drawn['probe/tiled/top.png']
    assert scale == (2.0, 2.0)


def test_a_legacy_brush_draws_its_migrated_record(renderer, context):
    """The two shipped _SHOWCASE brushes whose authored per-tag scale was not
    on their planes. Loaded as the editor loads a version 3 map, the
    precedence rule moves the authored value onto the plane and the GPU
    draws it; an unmigrated legacy dict handed straight to the renderer is
    read from its planes only -- nothing at run time consults a per-tag
    copy."""
    from editor.editor_state import EditorState
    from engine import brush_geometry as bg
    from tests.helpers.paths import REPO_ROOT
    names = _recording(renderer)
    showcase = {b['id']: b for b in bs.shipped_maps(REPO_ROOT)['_SHOWCASE.json']}
    raw = []
    for number, bid in enumerate(('5b10b49b-9b6c-45d9-91ee-0b6f06970590',
                                  '8c04e483-4d44-4217-a03d-32844a727043')):
        # In the frame the editor holds it in (Stage 2d), materials as stored.
        brush = {k: v for k, v in bs.localised(showcase[bid]).items()
                 if k not in bg.GEO_RUNTIME_KEYS}
        brush['name'] = 'showcase%d' % number
        for plane in brush['geometry']['planes']:
            tag = plane.get('face')
            key = tag or '#%d' % brush['geometry']['planes'].index(plane)
            plane['texture'] = 'probe/showcase%d/%s.png' % (number, key)
        bg.invalidate_geometry_cache(brush)
        bg.translate_brush(brush, np.subtract((-150 + 300 * number, 40, 0),
                                              brush['pos']).tolist())
        assert 'resolved_from_tag' in bs.inconsistencies(brush)
        raw.append({k: v for k, v in brush.items() if k not in bg.GEO_RUNTIME_KEYS})
    state = EditorState()
    # Saved as a version 3 file stores it: world-space planes.
    from engine.brush_placement import world_layout
    state.load_from_data({'version': 3, 'brushes': [world_layout(b) for b in raw],
                          'things': []}, stream_editor=False, save_undo=False)
    loaded = state.brushes
    drawn = _drawn_state(renderer, context, loaded, names)
    assert _check(renderer, loaded, drawn) > 0
    assert {(0.25, 0.25)} <= {s for s, _a, _h in drawn['probe/showcase0/north.png']}
    assert {(10.0, 8.0)} == {s for s, _a, _h in drawn['probe/showcase1/east.png']}
    unmigrated = _drawn_state(renderer, context, copy.deepcopy(raw), names)
    assert {(1.0, 1.0)} == {s for s, _a, _h in unmigrated['probe/showcase1/east.png']}


def test_save_and_reload_draw_the_same_geometry_and_uvs(renderer, context):
    """Through ``EditorState``: what a map draws after being saved and
    loaded again is what it drew -- the same triangles with the same UV
    state, and the same convex geometry."""
    from editor.editor_state import EditorState
    from engine import brush_faces as bf
    from engine import brush_geometry as bg
    from tests.helpers.paths import REPO_ROOT
    from tests.helpers.worlds import box_brush
    from tests.helpers.draw_oracle import DrawCapture, convex_programs
    names = _recording(renderer)
    edited = _unique_textures(_angled("edited", (0, 40, 0)), 'edited')
    from editor import face_texture as ft
    ft.set_transform(edited, 'top', shift=(0.3, 0.6), angle=15.0, scale=(2.0, 3.0))
    ft.set_transform(edited, _cut_key(edited), natural=True)
    box = box_brush("box", (300, 40, 0), (64, 64, 64), textures={'top': 'Dev/128.jpg'},
                    uv_shift={'top': [0.5, 0.0]})
    showcase = [b for b in bs.shipped_maps(REPO_ROOT)['_SHOWCASE.json']
                if bs.encoding(b) == 'convex']
    state = EditorState()
    state.load_from_data({'brushes': [edited, box] + showcase, 'things': []},
                         stream_editor=False, save_undo=False)
    saved = json.loads(json.dumps(state.get_level_data()))
    again = EditorState()
    again.load_from_data(saved, stream_editor=False, save_undo=False)
    assert [b['id'] for b in again.brushes] == [b['id'] for b in state.brushes]
    from engine.render_table import RenderTable
    tables = []
    for brushes in (state.brushes, again.brushes):
        table = RenderTable()
        table.sync(brushes, 1)
        tables.append(table)
    # The same material classification (pass, water, tiling ...) per brush.
    assert np.array_equal(tables[0].class_bits[:tables[0].count],
                          tables[1].class_bits[:tables[1].count])
    from engine.brush_placement import MAP_VERSION
    assert saved['version'] == MAP_VERSION
    for first, second in zip(state.brushes, again.brushes):
        assert bf.face_keys(first) == bf.face_keys(second), first.get('name')
        for key in bf.face_keys(first):
            assert bf.face_material(first, key) == bf.face_material(second, key), key
        if bs.encoding(first) == 'convex':
            assert np.array_equal(bg.get_convex(first).verts, bg.get_convex(second).verts)

    def capture(brushes):
        config, table, slots, camera = _setup(renderer, context, brushes, [], False)
        with DrawCapture(program=convex_programs(renderer),
                         skip_buffers=[renderer._cube_vbo]) as captured:
            _production(renderer, context, config, table, slots, camera, None)
        from tests.helpers.draw_oracle import _triangles_of_capture
        return sorted((key, model.tobytes()) for draw in captured.draws
                      for key, model, _normal in _triangles_of_capture(draw))

    # The textured pass's triangles (the solid pass draws flat colour; box
    # faces are compared by their records above).
    def textured(brushes):
        return [b for b in brushes if bs.draw_pass(b) == 'textured']

    first, second = capture(textured(state.brushes)), capture(textured(again.brushes))
    assert first and first == second


def _expected_by_texture(renderer, brushes):
    """``texture -> set of (scale, angle, shift)`` the records say is drawn
    (faces may share a texture, as a real map's do)."""
    from engine import brush_faces as bf
    out = {}
    for brush in brushes:
        if bs.draw_pass(brush) != 'textured' or bs.encoding(brush) != 'convex':
            continue
        for key, (_t, _r, material) in zip(bf.face_keys(brush), bs.render_faces(brush)):
            if material['texture'] in ('caulk.jpg',):
                continue
            out.setdefault(material['texture'], set()).add(
                _expected_state(renderer, brush, key, material))
    return out


def test_the_angled_brush_test_map_through_load_edit_save_and_reload(renderer, context):
    """``maps/Angled_Brush_Test.json`` -- ramps, wedges and a rotated brush
    with legacy per-tag copies -- as the editor loads it: every angled face
    draws its one record; Surface Inspector edits to a tagged and a cut face
    of each brush reach the GPU and nothing else moves; and the saved,
    reloaded map draws the same triangles with the same UV state."""
    from editor import face_texture as ft
    from editor.editor_state import EditorState
    from engine import brush_faces as bf
    from tests.helpers.draw_oracle import (DrawCapture, _triangles_of_capture,
                                           convex_programs)
    from tests.helpers.paths import repo_path
    names = _recording(renderer)
    with open(repo_path('maps', 'Angled_Brush_Test.json')) as handle:
        data = json.load(handle)
    state = EditorState()
    state.load_from_data({'brushes': data['brushes'], 'things': []},
                         stream_editor=False, save_undo=False)
    angled = [b for b in state.brushes if bs.encoding(b) == 'convex']
    assert len(angled) == 4
    for brush in angled:               # loaded with one record per face
        tags = {p.get('face') for p in brush['geometry']['planes'] if p.get('face')}
        assert not tags & set(brush.get('textures') or {})

    drawn = _drawn_state(renderer, context, state.brushes, names)
    assert drawn == _expected_by_texture(renderer, state.brushes)

    for number, brush in enumerate(angled):
        keys = bf.face_keys(brush)
        cut = next((k for k in keys if k.startswith('#')), None)  # the rotated one has none
        tagged = next(k for k in ('top', 'north', 'east', 'west', 'south', 'down')
                      if k in keys)
        for key in (tagged, cut) if cut else (tagged,):
            ft.set_transform(brush, key, shift=(0.1 * (number + 1), 0.05),
                             scale=(1.5 + number, 0.5), angle=15.0 * (number + 1))
    edited = _drawn_state(renderer, context, state.brushes, names)
    assert edited == _expected_by_texture(renderer, state.brushes)
    assert edited != drawn
    for number in range(4):
        wanted = (tuple(np.round(np.float32((1.5 + number, 0.5)), 5)),
                  round(float(np.float32(np.radians(15.0 * (number + 1)))), 5),
                  tuple(np.round(np.float32((0.1 * (number + 1), 0.05)), 5)))
        assert any(wanted in states for states in edited.values()), number

    saved = json.loads(json.dumps(state.get_level_data()))
    again = EditorState()
    again.load_from_data(saved, stream_editor=False, save_undo=False)

    def triangles(brushes):
        config, table, slots, camera = _setup(renderer, context, brushes, [], False)
        with DrawCapture(program=convex_programs(renderer),
                         skip_buffers=[renderer._cube_vbo]) as captured:
            _production(renderer, context, config, table, slots, camera, None)
        return sorted((key, model.tobytes()) for draw in captured.draws
                      for key, model, _normal in _triangles_of_capture(draw))

    before, after = triangles(state.brushes), triangles(again.brushes)
    assert before and before == after


# ---------------------------------------------------------------------------
# The caulk-only angled brush (the original M1/M2 parity fixture's scenario)
# ---------------------------------------------------------------------------

def _caulk_variants():
    """One angled brush, caulk on every plane, in three layouts, each loaded
    through ``EditorState`` as a map of its own version:

    * ``mixed`` -- the original parity fixture: legacy (version 3), every
      plane caulk, the per-tag dict still naming a real texture;
    * ``legacy`` -- legacy, both homes caulk;
    * ``canonical`` -- version 4, caulk on the planes only.

    And a caulk-only box beside them, the rule they are held to."""
    from editor.editor_state import EditorState
    from engine import brush_geometry as bg
    from tests.helpers.worlds import angled_brush, box_brush
    tags = ('south', 'north', 'west', 'east', 'down', 'top')

    def angled(per_tag):
        brush = angled_brush("caulked", (0.0, 40.0, 0.0), (80.0, 80.0, 80.0),
                             textures={t: per_tag for t in tags})
        for plane in brush['geometry']['planes']:
            plane['texture'] = 'caulk.jpg'
        bg.invalidate_geometry_cache(brush)
        brush['textures'] = {t: per_tag for t in tags}      # the legacy copy
        return {k: v for k, v in brush.items() if k not in bg.GEO_RUNTIME_KEYS}

    canonical = angled('caulk.jpg')
    canonical.pop('textures')
    out = {}
    for label, brush, version in (('mixed', angled('Dev/128.jpg'), 3),
                                  ('legacy', angled('caulk.jpg'), 3),
                                  ('canonical', canonical, 4)):
        state = EditorState()
        state.load_from_data({'version': version, 'brushes': [copy.deepcopy(brush)],
                              'things': []}, stream_editor=False, save_undo=False)
        out[label] = state.brushes[0]
    out['box'] = box_brush("caulked box", (0.0, 40.0, 0.0), (80.0, 80.0, 80.0),
                           textures={t: 'caulk.jpg' for t in tags})
    return out


def test_a_caulk_only_angled_brush_is_classified_and_collides_as_one(renderer, context):
    """All three layouts load to the same faces, the same classification as
    the caulk-only box (solid, not textured, a shadow caster, not water) and
    the same solid mesh collision."""
    from engine import brush_faces as bf
    from engine import brush_geometry as bg
    from engine import render_table as rt
    from engine.constants import is_water_brush
    from engine.render_table import RenderTable
    variants = _caulk_variants()
    angled = [variants[k] for k in ('mixed', 'legacy', 'canonical')]
    faces = [[bf.face_material(b, k) for k in bf.face_keys(b)] for b in angled]
    assert faces[0] == faces[1] == faces[2]
    assert all(m.texture == 'caulk.jpg' for m in faces[0])
    table = RenderTable()
    table.sync(angled + [variants['box']], 1)
    box_bits = int(table.class_bits[3])
    for slot in range(3):
        bits = int(table.class_bits[slot])
        assert bits & ~rt.CLASS_HAS_GEOMETRY == box_bits, slot
    assert not box_bits & rt.CLASS_TEXTURED and box_bits & rt.CLASS_SHADOW_CASTER
    triangles = []
    for brush in angled:
        assert not is_water_brush(brush)
        assert bg.build_collision_mesh(brush)
        triangles.append(brush['_mesh_triangles'])
    assert triangles[0] == triangles[1] == triangles[2]


@pytest.mark.parametrize("mode,play", (("Textured", False), ("Flat", False),
                                        ("Wireframe", False), ("Textured", True)))
def test_a_caulk_only_angled_brush_draws_as_one(renderer, context, mode, play):
    """Every layout draws the same pixels in each editor mode and in play,
    by the same programs as the caulk-only box: the solid (lit) pass in
    Textured mode -- caulk is never drawn by a textured program -- the flat
    pass, and its edges in Wireframe.

    Before Stage 1 the ``mixed`` layout reached the textured pass through its
    per-tag copy and drew nothing, unlike the other two; it now draws as a
    brush whose faces are all caulk does (box or angled)."""
    from tests.helpers.draw_oracle import DrawCapture, convex_programs
    variants = _caulk_variants()
    projection, view, eye = glh.camera_matrices(
        aspect=1.0, eye=(120.0, 200.0, 220.0), target=(0.0, 40.0, 0.0))

    def frame(brush):
        config = glh.render_config(all_brushes=[brush], all_things=[],
                                   play_mode=play, shadows_enabled=False,
                                   brush_display_mode=mode)
        context.bind()
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config['all_brush_slots'])
        with DrawCapture(program=convex_programs(renderer)) as textured:
            renderer.render_scene(projection, view, eye, None, config,
                                  brush_slots=config['all_brush_slots'])
        return context.read_pixels(), textured.calls

    images = {label: frame(variants[label]) for label in variants}
    reference, textured_calls = images['canonical']
    assert reference.any(), "a caulk-only brush is drawn in %s" % mode
    for label in ('mixed', 'legacy'):
        assert np.array_equal(images[label][0], reference), label
    for label, (image, calls) in images.items():
        assert calls == 0, "%s: caulk drawn by a textured program" % label
        assert image.any(), label


@pytest.mark.parametrize("case", ("plain", "scaled", "shifted and rotated", "natural"))
def test_box_to_convex_conversion_keeps_the_pixels(renderer, context, case):
    """A box and the angled brush converted from it draw the same pixels from
    every side: the same material record on every face, and the cube's own
    texture orientation on its two X-facing sides (the convex default for
    those turns the texture 90 degrees).

    Natural is compared on a box whose X-facing sides are square: the box
    path computes those sides' Natural repeats from swapped extents (it
    reads Z for the U axis, which runs along Y on the cube), a pre-existing
    box-path quirk left unchanged in this stage; the converted face draws the
    true constant texel size."""
    from engine import brush_geometry as bg
    from tests.helpers.worlds import box_brush
    tags = ('south', 'north', 'west', 'east', 'down', 'top')
    material = {
        'plain': {},
        'scaled': {'uv_scale': {t: [2.0, 3.0] for t in tags}},
        'shifted and rotated': {'uv_shift': {t: [0.25, 0.5] for t in tags},
                                'uv_angle': {t: 30.0 for t in tags}},
        'natural': {'uv_natural': {t: True for t in tags}},
    }[case]
    size = (160.0, 96.0, 96.0) if case == 'natural' else (160.0, 128.0, 96.0)
    box = box_brush("box", (0.0, 64.0, 0.0), size,
                    textures={t: 'Dev/128.jpg' for t in tags}, **copy.deepcopy(material))
    converted = copy.deepcopy(box)
    bg.box_to_geometry(converted)
    assert bs.encoding(converted) == 'convex'

    def frames(brush):
        config = glh.render_config(all_brushes=[brush], all_things=[],
                                   play_mode=False, shadows_enabled=False)
        out = []
        for eye in ((260.0, 220.0, 300.0), (-300.0, 160.0, -260.0), (0.0, 400.0, 1.0)):
            projection, view, eye_v = glh.camera_matrices(aspect=1.0, eye=eye,
                                                          target=(0.0, 64.0, 0.0))
            context.bind()
            renderer.render_scene(projection, view, eye_v, None, config,
                                  brush_slots=config['all_brush_slots'])
            out.append(context.read_pixels().astype(int))
        return out

    for index, (a, b) in enumerate(zip(frames(box), frames(converted))):
        differing = int((a != b).any(axis=2).sum())
        assert differing == 0, "view %d: %d pixels differ" % (index, differing)
