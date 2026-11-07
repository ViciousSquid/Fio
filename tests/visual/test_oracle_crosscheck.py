"""Visual tier: the two parity oracles, checked against each other.

* The **table oracle** (``tests/helpers/table_oracle``; what Debug Tables
  shows by hand) proves a published RenderTable equals one rebuilt from the
  live world.
* The **draw oracle** (``tests/helpers/draw_oracle`` with the frozen old path
  in ``tests/helpers/geo_reference``) proves what reached the GPU -- per
  triangle: vertex bytes, texture, UV state, transform, cull face -- is what
  the old renderer drew for the same brush dicts. It never reads a table.

Neither is trusted alone. On a clean frame both must pass. Each fault
injected below must be caught by the oracle whose ground it is on: a stale
table by both (the draw follows the table), and every fault introduced after
the table -- in the renderer's handle arrays, its face-run columns, a mesh's
per-vertex face state -- by the draw oracle, while the table oracle, rightly,
stays clean. That last group is why the second oracle exists.
"""

import ctypes

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.draw_oracle import DrawCapture, compare_geo_draws, convex_programs
from tests.helpers.geo_reference import expected_geo_draws
from tests.helpers.legacy_view import for_frozen_reference
from tests.helpers.table_oracle import render_table_diff

pytestmark = [pytest.mark.gl]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(128, 128) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    from tests.visual.test_geo_run_parity import distinct_texture_loader
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


def _world():
    from tests.helpers.worlds import angled_brush
    textures = ("Dev/128.jpg", "Dev/256.jpg", "Dev/512.jpg", "concrete02.png")
    brushes = []
    for i in range(5):
        brush = angled_brush("cross%d" % i, (-260 + 130 * i, 50, 0),
                             (100, 100, 100), clip_normal=(0.3 * i - 0.6, 1.0, 0.4))
        for p, plane in enumerate(brush['geometry']['planes']):
            plane['texture'] = textures[(i + p) % len(textures)]
            plane['uv_shift'] = [0.1 * p, 0.05 * i]
        from engine import brush_geometry as bg
        bg.invalidate_geometry_cache(brush)
        brushes.append(brush)
    return brushes


def _oracles(renderer, context, brushes, table, slots):
    """``(table oracle problems, draw oracle problems)`` for one frame."""
    import OpenGL.GL as gl
    config = glh.render_config(all_brushes=brushes, render_table=table,
                               all_brush_slots=slots, shadows_enabled=False)
    context.bind()
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    programs = convex_programs(renderer)
    with DrawCapture(program=programs, skip_buffers=[renderer._cube_vbo]) as cap:
        renderer.render_scene(*glh.camera_matrices(eye=(0, 380, 640),
                                                   target=(0, 20, 0)),
                              None, config, clear=False, brush_slots=slots)

    def gl_texture(name):
        return (renderer.texture_manager.get(renderer._tex_cache_path(name))
                or renderer.load_texture_callback(name, 'textures'))

    expected = expected_geo_draws(for_frozen_reference(brushes)[0], False, gl_texture,
                                  renderer._texture_pixel_size)
    return render_table_diff(table), compare_geo_draws(expected, cap.draws)


@pytest.fixture(params=('elements', 'off'), ids=('batched', 'per-mesh'))
def scene(renderer, context, request):
    from engine.render_table import RenderTable
    renderer.geo_batching = request.param
    brushes = _world()
    table = RenderTable()
    table.sync(brushes, 1)
    slots = np.arange(table.count, dtype=np.int32)
    return brushes, table, slots


def test_a_clean_frame_passes_both_oracles(renderer, context, scene):
    brushes, table, slots = scene
    for _ in range(2):
        table_problems, draw_problems = _oracles(renderer, context, *scene)
        assert table_problems == []
        assert draw_problems == [], "\n".join(draw_problems)


def test_a_stale_table_fails_both_oracles(renderer, context, scene):
    """A retexture the table was never told about (no journal, no epoch)."""
    from engine import brush_geometry as bg
    brushes, table, slots = scene
    _oracles(renderer, context, *scene)
    planes = brushes[2]['geometry']['planes']
    assert planes[0]['texture'] != 'Dev/128.jpg'
    planes[0]['texture'] = 'Dev/128.jpg'
    bg.invalidate_geometry_cache(brushes[2])
    table_problems, draw_problems = _oracles(renderer, context, *scene)
    assert table_problems, "the table oracle missed a stale row"
    assert draw_problems, "the draw oracle missed a stale row"


def test_swapped_handle_arrays_fail_only_the_draw_oracle(renderer, context, scene):
    """The renderer drawing handle 0's mesh for handle 1: tables are fine."""
    brushes, table, slots = scene
    _oracles(renderer, context, *scene)
    entry = renderer._geo_handles(table)
    for name, _dtype in type(entry).COLUMNS:
        column = getattr(entry, name)
        column[[0, 1]] = column[[1, 0]]
    entry.meshes[[0, 1]] = entry.meshes[[1, 0]]
    table_problems, draw_problems = _oracles(renderer, context, *scene)
    assert table_problems == []
    assert draw_problems, "the draw oracle missed a wrong handle-to-mesh mapping"


def test_a_wrong_face_texture_fails_only_the_draw_oracle(renderer, context, scene):
    brushes, table, slots = scene
    _oracles(renderer, context, *scene)
    entry = renderer._geo_handles(table)
    arena = renderer._geo_runs()
    row = int(entry.run_base[2])
    used = arena.texture[:arena.used]
    arena.texture[row] = used[used != used[row]][0]
    table_problems, draw_problems = _oracles(renderer, context, *scene)
    assert table_problems == []
    assert draw_problems, "the draw oracle missed a face bound to the wrong texture"


def test_wrong_per_vertex_face_state_fails_only_the_draw_oracle(
        renderer, context, scene):
    import OpenGL.GL as gl
    if 'geo_textured' not in renderer.shaders:
        pytest.skip("no per-vertex face program on this driver")
    brushes, table, slots = scene
    _oracles(renderer, context, *scene)
    from engine.renderer.core import geo_store
    mesh = renderer._geo_handles(table).meshes[3]
    # One vertex's UV shift, in the shared page, replaced.
    bad = (ctypes.c_float * 2)(0.25, 0.75)
    page = renderer._geo_store().pages[mesh.page]
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, page.vbo)
    gl.glBufferSubData(gl.GL_ARRAY_BUFFER, mesh.start * geo_store.STRIDE + 44,
                       8, bad)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
    table_problems, draw_problems = _oracles(renderer, context, *scene)
    assert table_problems == []
    assert draw_problems, "the draw oracle missed corrupted per-vertex face state"


@pytest.mark.integration
def test_a_scripted_session_passes_both_oracles(renderer, context):
    """Editor edits, undo, a runtime hide and Play, through the real pipeline.

    EditorState -> LogicThread -> double-buffered publication -> renderer.
    Every published frame (both buffers) must pass the table oracle, and the
    convex draws it produces must match the frozen old path run on the live
    brush dicts.
    """
    import OpenGL.GL as gl
    from editor import face_texture as ft
    from editor.editor_state import EditorState
    from engine import brush_geometry as bg
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.spatial import set_authored_flag
    from engine.threaded_game_state import ThreadedGameState
    from tests.helpers.table_oracle import diff_rebuild

    state = EditorState()
    state.brushes = _world()
    for brush in state.brushes[:2]:
        brush['geometry']['planes'][2]['texture'] = 'nodraw.jpg'
        bg.invalidate_geometry_cache(brush)
    state.mark_world_changed()
    game_state = ThreadedGameState()
    logic = LogicThread(game_state, state)
    logic.render_runtime.culling_enabled = False

    def gl_texture(name):
        return (renderer.texture_manager.get(renderer._tex_cache_path(name))
                or renderer.load_texture_callback(name, 'textures'))

    def check(step, play=False):
        for _ in range(2):                      # both render buffers
            logic._step_frame(logic.TICK_DURATION)
            logic._publish_frame()
            game_state.try_swap()
            frame = game_state.get_render_state()
            try:
                assert diff_rebuild(frame) == [], step
                config = glh.render_config(
                    render_table=frame.render_table,
                    render_refs=frame.render_refs,
                    all_brush_slots=frame.all_brush_slots,
                    visible_brush_slots=frame.visible_brush_slots,
                    entity_table=frame.entity_table,
                    entity_refs=frame.entity_refs,
                    visible_thing_slots=frame.visible_thing_slots,
                    thing_hidden=frame.thing_hidden,
                    play_mode=play, shadows_enabled=False)
                context.bind()
                gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
                programs = convex_programs(renderer)
                with DrawCapture(program=programs,
                                 skip_buffers=[renderer._cube_vbo]) as cap:
                    renderer.render_scene(
                        *glh.camera_matrices(eye=(0, 380, 640), target=(0, 20, 0)),
                        None, config, clear=False,
                        brush_slots=frame.all_brush_slots)
                shown = [b for b in state.brushes if not b.get('hidden')]
                expected = expected_geo_draws(for_frozen_reference(shown)[0], play,
                                              gl_texture, renderer._texture_pixel_size)
                assert expected, "%s: the session draws nothing" % step
                problems = compare_geo_draws(expected, cap.draws)
                assert problems == [], "%s:\n%s" % (step, "\n".join(problems))
            finally:
                game_state.release_render_state(frame)

    try:
        check("initial")
        brush = state.brushes[1]
        key = ft.face_keys(brush)[-1]
        ft.set_transform(brush, key, texture='concrete02.png')
        state.mark_world_changed([brush])
        check("retextured a cut face")
        ft.set_transform(brush, ft.face_keys(brush)[0], shift=(0.3, 0.1), angle=30.0)
        state.mark_world_changed([brush])
        check("moved and rotated a face's texture")
        state.save_state()
        bg.translate_brush(state.brushes[3], (24.0, 0.0, -8.0))
        state.mark_world_changed([state.brushes[3]])
        check("moved a brush")
        state.undo()
        check("undo")
        set_authored_flag(state.brushes[4], 'hidden', True)
        check("hidden at run time")
        logic.player_runtime.player = Player(0.0, 0.0, 400.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        check("play (nodraw dropped)", play=True)
    finally:
        logic.stop()


def test_a_wrong_transform_slot_fails_only_the_draw_oracle(renderer, context, scene):
    """A brush's transform in the buffer texture, changed behind the mirror."""
    import OpenGL.GL as gl
    from engine.renderer.core import geo_store
    if renderer.geo_batching == 'off' or 'geo_batched' not in renderer.shaders:
        pytest.skip("only the batched path reads transforms from the buffer")
    brushes, table, slots = scene
    _oracles(renderer, context, *scene)
    store = renderer._geo_store()
    slot = int(renderer._geo_handles(table).slot[2])
    shifted = store.mirror[slot].copy()
    shifted[3, 0] += 8.0                 # the model matrix's x translation
    gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, store.transform_buffer)
    gl.glBufferSubData(gl.GL_TEXTURE_BUFFER,
                       slot * geo_store.TEXELS_PER_SLOT * 16, shifted.nbytes, shifted)
    gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, 0)
    table_problems, draw_problems = _oracles(renderer, context, *scene)
    assert table_problems == []
    assert draw_problems, "the draw oracle missed a brush drawn with a wrong transform"
