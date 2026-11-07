"""Visual tier: the brush-semantics oracle agrees with the GPU's own state.

``tests/brush_model`` pins what brushes mean today through
``tests/helpers/brush_semantics``, tied there to the engine's tables and to
the frozen copy of the convex path. This file ends the chain at the GPU for
the cases that matter most to the unified brush model -- the faces whose
material is stored twice and disagrees: every triangle the textured pass
draws for them is read back by the draw oracle (bound texture, and the UV
scale, angle and shift the program will use) and compared with what the
oracle says the face renders.

So "the Surface Inspector's edit of a tagged angled face does not render" is
a statement about the GPU, not about a helper's reading of the dicts.
"""

import math

import numpy as np
import pytest

from tests.helpers import brush_semantics as bs
from tests.helpers import gl as glh
from tests.visual.test_geo_run_parity import (  # noqa: F401 (fixtures)
    _production, _setup, context, renderer)

pytestmark = [pytest.mark.gl, pytest.mark.qt]


def _probe_brushes():
    """Angled brushes whose two material homes disagree, each face with a
    texture of its own so a drawn triangle names its face."""
    from editor import face_texture as ft
    from engine import brush_geometry as bg
    from tests.helpers.worlds import angled_brush, box_brush
    from tests.helpers.paths import REPO_ROOT

    def unique_textures(brush, label):
        for index, plane in enumerate(brush['geometry']['planes']):
            name = 'probe/%s/%d.png' % (label, index)
            plane['texture'] = name
            if plane.get('face'):
                brush.setdefault('textures', {})[plane['face']] = name
        bg.invalidate_geometry_cache(brush)
        return brush

    converted = box_brush("converted", (-160, 40, 0), (96, 80, 96),
                          uv_shift={'top': [0.25, 0.5], 'north': [0.1, 0.0]},
                          uv_angle={'top': 30.0, 'north': 60.0},
                          uv_scale={'top': [2.0, 2.0]})
    bg.box_to_geometry(converted)
    unique_textures(converted, 'converted')

    inspected = angled_brush("inspected", (0, 40, 0), (96, 80, 96),
                             clip_normal=(0.3, 1.0, -0.2))
    unique_textures(inspected, 'inspected')
    for tag in ('top', 'north', 'east'):
        ft.set_transform(inspected, tag, shift=(0.75, 0.0), angle=45.0,
                         scale=(4.0, 4.0))

    cut = angled_brush("cut", (160, 40, 0), (96, 80, 96),
                       clip_normal=(-0.4, 1.0, 0.3))
    unique_textures(cut, 'cut')
    index = next(i for i, p in enumerate(cut['geometry']['planes'])
                 if not p.get('face'))
    ft.set_transform(cut, '#%d' % index, scale=(3.0, 1.5))
    ft.set_transform(cut, '#%d' % index, natural=True)

    tiled = angled_brush("tiled", (0, 40, 160), (256, 32, 96), texture_tiling=True)
    unique_textures(tiled, 'tiled')

    showcase = [b for b in bs.shipped_maps(REPO_ROOT)['_SHOWCASE.json']
                if 'material_mismatch' in bs.inconsistencies(b)]
    assert len(showcase) == 4
    moved = []
    for number, brush in enumerate(showcase):
        brush = _copy(brush)
        unique_textures(brush, 'showcase%d' % number)
        bg.translate_brush(brush, np.subtract((-160 + 110 * number, 40, -200),
                                              brush['pos']).tolist())
        moved.append(brush)
    brushes = [converted, inspected, cut, tiled] + moved
    for brush in brushes:
        assert bs.inconsistencies(brush), brush['name']
    return brushes


def _copy(brush):
    import copy
    from engine import brush_geometry as bg
    return copy.deepcopy({k: v for k, v in brush.items()
                          if k not in bg.GEO_RUNTIME_KEYS})


def _expected_by_texture(brushes):
    """``texture name -> material`` for every face the oracle says draws."""
    out = {}
    for brush in brushes:
        for _tag, _ring, material in bs.render_faces(brush):
            out[material['texture']] = material
    return out


@pytest.mark.parametrize("batching", ("elements", "off"))
def test_disagreeing_faces_draw_with_the_material_the_oracle_names(
        renderer, context, batching):
    from tests.helpers.draw_oracle import (DrawCapture, _f32,
                                           _triangles_of_capture, convex_programs)
    renderer.geo_batching = batching
    names = {}
    load = renderer.load_texture_callback

    def recording(name, subfolder):
        tex = load(name, subfolder)
        names[int(tex)] = name
        return tex

    renderer.load_texture_callback = recording
    brushes = _probe_brushes()
    config, table, slots, camera = _setup(renderer, context, brushes, [], False)
    with DrawCapture(program=convex_programs(renderer),
                     skip_buffers=[renderer._cube_vbo]) as capture:
        _production(renderer, context, config, table, slots, camera, None)
    expected = _expected_by_texture(brushes)
    seen = set()
    for draw in capture.draws:
        for (vertices, texture, _cull, scale, angle, shift), _m, _n in \
                _triangles_of_capture(draw):
            name = names[texture]
            material = expected[name]
            seen.add(name)
            if not material['natural']:
                assert tuple(_f32(scale)) == pytest.approx(
                    material['uv_scale'] or (1.0, 1.0)), name
            assert float(_f32(angle)[0]) == pytest.approx(
                math.radians(material['uv_angle'])), name
            assert tuple(_f32(shift)) == pytest.approx(material['uv_shift']), name
    # Every face of every probe was drawn and checked (none is caulk/nodraw).
    assert seen == set(expected)


def test_a_convex_brush_textured_only_on_its_planes_reaches_no_textured_program(
        renderer, context):
    """The GPU side of ``untextured_planes``: such a brush is drawn by the
    lit (solid) program and never by a program that samples a texture."""
    from engine import brush_geometry as bg
    from tests.helpers.draw_oracle import DrawCapture, convex_programs
    from tests.helpers.worlds import box_brush
    brush = box_brush("plain", (0, 40, 0), (128, 128, 128))
    brush.pop('textures', None)
    assert bg.clip_brush(brush, [0.3, 1.0, 0.2], 60.0, texture='Dev/128.jpg')
    assert bs.draw_pass(brush) == 'solid'
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
    assert textured.calls == 0
    assert solid.calls > 0
