"""The particle emitter pass, on a real GL 3.3 core context.

One instanced draw for every emitter in view, nothing for an Effect without
one; GL state left as it was found; emitter rows uploaded only when they
change; the decal pass's depth copy reused for soft particles.
"""

import glm
import numpy as np
import pytest

from tests.helpers import gl as glh

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")

pytestmark = [pytest.mark.gl]

W, H = 160, 120
EYE = glm.vec3(0.0, 60.0, 160.0)


@pytest.fixture
def renderer():
    glh.reset_texture_cache()
    with glh.GLTestContext(W, H) as ctx:
        made = glh.make_renderer()
        made.shadows_enabled = False
        yield ctx, made
        made.cleanup()
    glh.reset_texture_cache()


def _effect(pos=(0.0, 20.0, -100.0), **props):
    from editor.things import Effect
    effect = Effect(properties=props)
    effect.pos = list(pos)
    return effect


def _matrices():
    projection = glm.perspective(glm.radians(70.0), W / H, 1.0, 5000.0)
    view = glm.lookAt(EYE, glm.vec3(0.0, 50.0, -100.0), glm.vec3(0.0, 1.0, 0.0))
    return projection, view


def _render(ctx, made, things, brushes=()):
    import OpenGL.GL as gl
    config = glh.render_config(all_things=list(things), all_brushes=list(brushes),
                               shadows_enabled=False)
    projection, view = _matrices()
    ctx.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    made.render_scene(projection, view, EYE, None, config,
                      brush_slots=config["all_brush_slots"])
    glh.no_gl_errors("render_scene")
    return ctx.read_pixels(), made.render_stats.draw_calls


def _table(things):
    from engine.entity_table import EntityTable
    table = EntityTable()
    table.begin_frame(list(things), epoch=1, effect_runtime=False)
    return table, np.asarray(table.effect_slots, dtype=np.int32)


def test_the_particle_program_compiles(renderer):
    _, made = renderer
    assert 'particle_instanced' in made.shaders
    for name in ('uPresets', 'uTime', 'uSceneDepth', 'uAmbient'):
        assert made.uniforms['particle_instanced'][name] >= 0, name


def test_an_effect_without_an_emitter_adds_no_draw(renderer):
    ctx, made = renderer
    _, plain_draws = _render(ctx, made, [_effect(effect_type="FIRE")])
    assert made._particle_uploaded is None            # the pass never ran
    _, smoke_draws = _render(ctx, made, [_effect(effect_type="FIRE", emitter="SMOKE")])
    assert smoke_draws == plain_draws + 1
    assert made._particle_uploaded is not None


def test_every_emitter_is_one_draw(renderer):
    ctx, made = renderer
    _, none = _render(ctx, made, [])
    things = [
        _effect((-60.0, 20.0, -100.0), effect_type="FIRE", emitter="SMOKE"),
        _effect((60.0, 5.0, -100.0), effect_type="PARTICLES", emitter="SPLASH",
                width=120.0, height=4.0),
        _effect((60.0, 15.0, -100.0), effect_type="PARTICLES", emitter="MIST",
                width=160.0, height=20.0),
    ]
    image, draws = _render(ctx, made, things[1:])
    assert draws == none + 1
    assert not glh.is_blank(image)
    _, draws = _render(ctx, made, things)
    # The FIRE's flame is one draw of its own; all three emitters are one.
    assert draws == none + 2


def test_particles_draw_no_gif_flame(renderer):
    ctx, made = renderer
    _render(ctx, made, [_effect(effect_type="PARTICLES", emitter="STEAM")])
    assert made.effect_fire_frames == {} and made.effect_orb_frames == {}


def test_smoke_shows_above_the_flame(renderer):
    ctx, made = renderer
    plain, _ = _render(ctx, made, [_effect(effect_type="FIRE", emitter="NONE",
                                           light_enabled=False)])
    smoke, _ = _render(ctx, made, [_effect(effect_type="FIRE", emitter="SMOKE",
                                           light_enabled=False)])
    # The top half of the view is above the flame (and holds no flame).
    top = slice(0, H // 3)
    assert glh.luminance(smoke[top]).sum() > glh.luminance(plain[top]).sum() + 50


def test_gl_state_is_put_back(renderer):
    import OpenGL.GL as gl
    ctx, made = renderer
    table, slots = _table([_effect(effect_type="PARTICLES", emitter="SPARKS")])
    projection, view = _matrices()
    ctx.bind()
    gl.glEnable(gl.GL_CULL_FACE)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glDepthMask(gl.GL_TRUE)
    gl.glDisable(gl.GL_BLEND)
    gl.glBlendFuncSeparate(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA,
                           gl.GL_ONE, gl.GL_ZERO)
    gl.glActiveTexture(gl.GL_TEXTURE0)
    assert made.draw_particles_instanced(projection, view, table, slots,
                                         play_mode=False, camera_pos=EYE) == 1
    glh.no_gl_errors("draw_particles_instanced")
    assert gl.glIsEnabled(gl.GL_CULL_FACE)
    assert gl.glIsEnabled(gl.GL_DEPTH_TEST)
    assert not gl.glIsEnabled(gl.GL_BLEND)
    assert bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
    assert [int(gl.glGetIntegerv(name)) for name in (
        gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
        gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA)] == [
        gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA, gl.GL_ONE, gl.GL_ZERO]
    assert int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)) == gl.GL_TEXTURE0


def test_rows_are_uploaded_only_when_they_change(renderer):
    ctx, made = renderer
    effect = _effect(effect_type="PARTICLES", emitter="SMOKE")
    table, slots = _table([effect])
    projection, view = _matrices()
    ctx.bind()
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    first = made._particle_uploaded
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    assert made._particle_uploaded is first          # the clock is a uniform
    table.effect_emitter_params[slots[0], 0] = 0.5
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    assert made._particle_uploaded is not first
    assert made._particle_uploaded[0, 5] == 24.0     # half of 48 particles


def test_emitters_are_drawn_back_to_front(renderer):
    ctx, made = renderer
    near = _effect((0.0, 20.0, 60.0), effect_type="PARTICLES", emitter="MIST")
    far = _effect((0.0, 20.0, -600.0), effect_type="PARTICLES", emitter="MIST")
    table, slots = _table([near, far])
    projection, view = _matrices()
    ctx.bind()
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    rows = made._particle_uploaded
    assert rows[0, 2] == pytest.approx(-600.0) and rows[1, 2] == pytest.approx(60.0)


def test_a_burst_shows_in_the_editor_only_as_a_frozen_preview(renderer):
    ctx, made = renderer
    projection, view = _matrices()
    ctx.bind()
    dormant = _effect(effect_type="EXPLOSION", emitter="SPARKS")
    table, slots = _table([dormant])
    assert made.draw_particles_instanced(projection, view, table, slots,
                                         play_mode=False, camera_pos=EYE) == 0
    preview = _effect(effect_type="EXPLOSION", emitter="SPARKS", preview=True)
    table, slots = _table([preview])
    assert made.draw_particles_instanced(projection, view, table, slots,
                                         play_mode=False, camera_pos=EYE) == 1
    assert made._particle_uploaded[0, 7] == 2.0      # frozen burst


def test_soft_particles_reuse_the_decal_depth_copy(renderer, monkeypatch):
    ctx, made = renderer
    table, slots = _table([_effect(effect_type="PARTICLES", emitter="SMOKE")])
    projection, view = _matrices()
    calls = []
    real = made._capture_scene_depth
    monkeypatch.setattr(made, "_capture_scene_depth",
                        lambda: calls.append(1) or real())
    ctx.bind()
    made._scene_depth_current = True                 # the decal pass made one
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    assert calls == []
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    assert calls == [1]                              # none this frame: copied
    glh.no_gl_errors("soft particles")


def test_particles_write_no_depth(renderer):
    import OpenGL.GL as gl
    ctx, made = renderer
    table, slots = _table([_effect((0.0, 50.0, -100.0), effect_type="PARTICLES",
                                   emitter="MIST", width=200.0, height=60.0)])
    projection, view = _matrices()
    ctx.bind()
    gl.glClearDepth(1.0)
    gl.glClear(gl.GL_DEPTH_BUFFER_BIT | gl.GL_COLOR_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    depth = gl.glReadPixels(0, 0, W, H, gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT)
    assert np.all(np.asarray(depth, dtype=np.float32) == 1.0)
    assert not glh.is_blank(ctx.read_pixels())
