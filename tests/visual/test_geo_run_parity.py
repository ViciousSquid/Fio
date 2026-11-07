"""Visual tier: angled brushes reach the GPU exactly as the old path sent them.

The textured pass draws convex (angled) brushes face by face. Every face's
texture and UV state used to be resolved per face per frame from the cached
mesh's run dicts (texture-name compares, texture-manager lookups, dict reads)
and sent as three uniforms before its own draw, in brush order. It is now
resolved once, when the mesh is built: the texture and caulk/nodraw bits into
the renderer's face-run columns, the UV state into the mesh's vertex data.
Faces are submitted sorted by texture, and adjacent faces of one mesh that
share a texture are one draw. This file proves the change made no difference
to what is drawn, with an oracle that does not read any table:

* **Draws.** ``tests/helpers/draw_oracle.DrawCapture`` reads back, at every
  draw, the vertex bytes it covers, the bound texture, the cull state and the
  UV state the GPU will use -- uniform values, or per-vertex attributes for a
  program that takes them so. ``tests/helpers/geo_reference.expected_geo_draws``
  derives what the old loop drew straight from the brush dicts (a frozen copy
  of its code). The two must match as multisets of triangles -- bit for bit
  except the matrices, which the reference derives independently and so
  compares to float32 precision -- and a ``GL_PRIMITIVES_GENERATED`` query
  must count exactly the reference's triangles.
* **Pixels.** The same frame is drawn by the production pass and by the frozen
  old loop with identical program and uniform state; the images must be
  identical.

Both run in the editor (nodraw visible) and in play (nodraw dropped), with
back-face culling as the main view and the portal views set it, and without.

This is M1/M2 *batching* parity, and the frozen path is not modified for
later changes. Since Stage 1 of the unified brush model a face's material
lives on its plane, so the frozen path is handed each brush in the layout
it reads (``tests/helpers/legacy_view``: the explicit legacy export).
Stage 1's own rendering changes are proved separately, against the GPU, in
``test_brush_semantics_gl.py``.
"""

import os
import zlib

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.legacy_view import for_frozen_reference

pytestmark = [pytest.mark.gl]

SIZE = 256


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def distinct_texture_loader(renderer):
    """Every texture name gets its own 4x4 texture and its own pixel size.

    The suite's usual loader serves one white texture for everything, which
    would hide a run bound to the wrong texture. Here each name has distinct
    texels (so a wrong bind, or a wrong UV transform, changes pixels) and a
    distinct reported size (so natural-scale faces depend on the right one).
    """
    import OpenGL.GL as gl
    cache = {}
    dims = renderer.__dict__.setdefault('_texture_dimensions', {})

    def loader(name, subfolder):
        if name in cache:
            return cache[name]
        h = zlib.crc32(name.encode())
        texels = bytes(((h >> (i % 24)) * (i + 7) + 40 * (i % 3)) & 0xFF
                       if i % 4 != 3 else 255 for i in range(64))
        tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 4, 4, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, texels)
        for param, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
        dims[os.path.join(subfolder, name)] = (32 * (1 + h % 4),
                                              16 * (1 + (h >> 2) % 4))
        cache[name] = tex
        return tex

    return loader, cache


@pytest.fixture
def renderer(context):
    from engine.renderer.forward import ForwardRenderer
    made = ForwardRenderer(None)
    loader, cache = distinct_texture_loader(made)
    made.load_texture_callback = loader
    made.shadows_enabled = False
    made.set_grid(4096, 64)
    made.set_sprite_textures({})
    try:
        yield made
    finally:
        import OpenGL.GL as gl
        if cache:
            gl.glDeleteTextures(sorted(set(cache.values())))
        made.cleanup()


def angled_scene():
    """A field of clipped and rotated brushes, plus the edge cases.

    The field is the benchmark's generator (varied textures, caulk and nodraw
    faces, authored scale/angle/shift, natural-scale faces). Added: a brush
    rotated as a mover is (its runtime spin: a rotated model matrix), a brush
    that is caulk but for one face (its caulk faces draw nothing) and one
    with an untextured cut face (``default.png``).
    """
    from engine import brush_geometry as bg
    from tools.bench_geo_passes import angled_world
    from tests.helpers import runtime
    from tests.helpers.worlds import angled_brush
    from editor.things import Light
    from tests.helpers.worlds import make_thing

    brushes = [b for b in angled_world(grid=5, spacing=150.0, seed=11)
               if b.get('geometry')]
    spun = angled_brush("spun", (40.0, 260.0, -60.0), (90.0, 70.0, 110.0),
                        clip_normal=(0.3, 1.0, -0.4))
    spun['rot_axis'] = [0.2, 1.0, 0.1]
    runtime.drive(spun, spin=((0.2, 1.0, 0.1), 33.0))
    caulked = angled_brush("caulked", (-300.0, 40.0, 300.0), (80.0, 80.0, 80.0))
    for plane in caulked['geometry']['planes']:
        plane['texture'] = 'caulk.jpg'
    # Its purpose here is caulk faces inside the textured pass, so it needs a
    # genuinely textured face: since Stage 1 the pass is decided by the faces'
    # own records, and a brush whose every face is caulk goes to the solid
    # pass like a caulk-only box (before, this fixture reached the textured
    # pass only through the per-tag texture copy its planes contradicted).
    # The caulk-only brush itself is tested on its own in
    # test_brush_semantics_gl.py::test_a_caulk_only_angled_brush_*.
    caulked['geometry']['planes'][-1]['texture'] = 'Dev/128.jpg'
    bg.invalidate_geometry_cache(caulked)
    bare = angled_brush("bare", (300.0, 60.0, 300.0), (100.0, 120.0, 90.0),
                        clip_normal=(-1.0, 0.8, 0.2))
    bare['geometry']['planes'][-1].pop('texture', None)
    bg.invalidate_geometry_cache(bare)
    brushes += [spun, caulked, bare]
    light = make_thing(Light, "geo_light", (0.0, 500.0, 200.0),
                       color=[255, 240, 220], intensity=2.0, radius=3000.0,
                       state="on", casts_shadows=False)
    return brushes, [light]


def _camera():
    return glh.camera_matrices(aspect=1.0, eye=(0.0, 520.0, 720.0),
                               target=(0.0, 40.0, 0.0))


def _gl_texture(renderer):
    """The old loop's per-run resolution of a texture name to a GL name."""
    def resolve(name):
        return (renderer.texture_manager.get(renderer._tex_cache_path(name))
                or renderer.load_texture_callback(name, 'textures'))
    return resolve


def _setup(renderer, context, brushes, things, play_mode):
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               play_mode=play_mode, shadows_enabled=False)
    table = config['render_table']
    assert table.ids == [b['id'] for b in brushes], (
        "the fixture relies on slot i being brushes[i]")
    projection, view, eye = _camera()
    context.bind()
    # One full frame first: every per-frame uniform (fog, environment) and
    # every texture is then in the state the isolated passes below share.
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=config['all_brush_slots'])
    slots = config['all_brush_slots']
    groups = renderer._classify_brush_slots(table, slots, config)
    textured = groups['textured']
    assert sorted(textured.tolist()) == list(range(len(brushes))), (
        "every fixture brush must go to the textured pass")
    return config, table, textured, (projection, view, eye)


def _clear(context):
    import OpenGL.GL as gl
    context.bind()
    gl.glClearColor(0.05, 0.05, 0.08, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT
               | gl.GL_STENCIL_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glDepthFunc(gl.GL_LESS)
    gl.glDepthMask(gl.GL_TRUE)


def _production(renderer, context, config, table, slots, camera, cull):
    projection, view, eye = camera
    _clear(context)
    renderer._frame_lights_uploaded.clear()
    renderer.render_stats.reset()
    lights = renderer._get_active_lights(config)
    renderer._opaque_cull_pass = cull == 'opaque'
    renderer._portal_scene_pass = cull == 'portal'
    try:
        renderer.draw_textured_brushes_optimized(
            projection, view, eye, slots, lights, config, table)
    finally:
        renderer._opaque_cull_pass = False
        renderer._portal_scene_pass = False
    return context.read_pixels()


def _reference(renderer, context, config, brushes, camera, cull, play_mode):
    """The frozen old loop, from the brush dicts, under the pass's set-up."""
    import glm
    import OpenGL.GL as gl
    from tests.helpers.geo_reference import ReferenceGeoDrawer
    projection, view, eye = camera
    _clear(context)
    renderer._frame_lights_uploaded.clear()
    lights = renderer._get_active_lights(config)
    shader, uniforms = renderer.shaders['textured'], renderer.uniforms['textured']
    gl.glUseProgram(shader)
    renderer._current_shader = shader
    renderer._upload_lights_once('textured', lights)
    gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                          glm.value_ptr(projection))
    gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
    gl.glActiveTexture(gl.GL_TEXTURE0)
    gl.glUniform1i(uniforms['texture_diffuse'], 0)
    gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
    if cull:
        # What the pass does before its convex half when interiors are culled:
        # convex meshes wind counter-clockwise outward, so GL_BACK.
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
    drawer = ReferenceGeoDrawer()
    try:
        drawer.draw(renderer, for_frozen_reference(brushes)[0], play_mode,
                    _gl_texture(renderer), renderer._texture_pixel_size)
    finally:
        drawer.release()
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
    return context.read_pixels()


CULL_MODES = (None, 'opaque', 'portal')
BATCHING = ('elements', 'multidraw', 'off')


@pytest.mark.parametrize("batching", BATCHING)
@pytest.mark.parametrize("play_mode", (False, True), ids=("editor", "play"))
@pytest.mark.parametrize("cull", CULL_MODES, ids=("no-cull", "main-view", "portal-view"))
def test_angled_brush_draws_match_the_frozen_old_path(renderer, context,
                                                      play_mode, cull, batching):
    import OpenGL.GL as gl
    from tests.helpers.draw_oracle import (DrawCapture, compare_geo_draws,
                                           convex_programs)
    from tests.helpers.geo_reference import expected_geo_draws

    renderer.geo_batching = batching
    brushes, things = angled_scene()
    config, table, slots, camera = _setup(renderer, context, brushes, things,
                                          play_mode)
    programs = convex_programs(renderer)
    with DrawCapture(program=programs,
                     skip_buffers=[renderer._cube_vbo]) as capture:
        _production(renderer, context, config, table, slots, camera, cull)
    expected = expected_geo_draws(
        for_frozen_reference([brushes[int(s)] for s in slots])[0], play_mode,
        _gl_texture(renderer), renderer._texture_pixel_size)
    assert expected, "the fixture draws nothing; the test exercises nothing"
    assert any(d['texture'] for d in expected)
    want_cull = gl.GL_BACK if cull else 0
    problems = compare_geo_draws(expected, capture.draws, cull=want_cull)
    assert problems == [], "\n".join(problems)
    triangles = sum(d['count'] // 3 for d in expected)
    assert capture.primitives == triangles, (
        "GL generated %d primitives; the old path draws %d triangles"
        % (capture.primitives, triangles))
    stats = renderer.render_stats
    assert stats.draw_calls == capture.calls <= len(expected)
    assert stats.visible_tris == triangles
    if batching != 'off':
        # One draw call per (texture, page): the per-brush draws are gone.
        textures = len({d['texture'] for d in expected})
        assert capture.calls <= textures


@pytest.mark.parametrize("batching", BATCHING)
@pytest.mark.parametrize("play_mode", (False, True), ids=("editor", "play"))
@pytest.mark.parametrize("cull", CULL_MODES, ids=("no-cull", "main-view", "portal-view"))
def test_angled_brush_pixels_match_the_frozen_old_path(renderer, context,
                                                       play_mode, cull, batching):
    renderer.geo_batching = batching
    brushes, things = angled_scene()
    config, table, slots, camera = _setup(renderer, context, brushes, things,
                                          play_mode)
    produced = _production(renderer, context, config, table, slots, camera, cull)
    reference = _reference(renderer, context, config,
                           [brushes[int(s)] for s in slots], camera,
                           bool(cull), play_mode)
    assert not glh.is_blank(reference), "the reference frame is empty"
    differing = np.any(produced != reference, axis=2)
    assert not differing.any(), (
        "%d pixels differ from the frozen old path" % int(differing.sum()))
