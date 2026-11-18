"""The EXPLOSION pass, on a real GL 3.3 core context.

The explosion was invisible: its instance rows put the height where the
shader read the Preview flag, so every card was 0.01 units tall. Pinned here:
it draws, in the editor's Preview and when triggered in play; it is a full
fireball seen from above (it faces the camera every way); it fades softly
where it meets the scene; it shares one depth copy with the particles; its
rows are uploaded only when they change; and it puts GL state back.
"""

import time

import glm
import numpy as np
import pytest

from tests.helpers import gl as glh

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")

pytestmark = [pytest.mark.gl]

W, H = 160, 120
EYE = glm.vec3(0.0, 40.0, 120.0)
TARGET = glm.vec3(0.0, 40.0, -100.0)
#: A pixel at least this bright (0..255 luminance) is fireball, not clear.
LIT = 40.0


@pytest.fixture
def renderer():
    glh.reset_texture_cache()
    with glh.GLTestContext(W, H) as ctx:
        made = glh.make_renderer()
        made.shadows_enabled = False
        if not made.effect_explosion_texture:
            pytest.skip("no explosion sprite sheet")
        yield ctx, made
        made.cleanup()
    glh.reset_texture_cache()


def _explosion(pos=(0.0, 0.0, -100.0), **props):
    from editor.things import Effect
    props.setdefault("effect_type", "EXPLOSION")
    props.setdefault("width", 48.0)
    props.setdefault("height", 48.0)
    props.setdefault("light_enabled", False)
    effect = Effect(properties=props)
    effect.pos = list(pos)
    return effect


def _matrices(eye=EYE, target=TARGET, up=(0.0, 1.0, 0.0)):
    projection = glm.perspective(glm.radians(70.0), W / H, 1.0, 5000.0)
    return projection, glm.lookAt(eye, target, glm.vec3(*up))


def _table(things, runtime=False):
    from engine.entity_table import EntityTable
    table = EntityTable()
    table.begin_frame(list(things), epoch=1, effect_runtime=runtime)
    return table, np.asarray(table.effect_slots, dtype=np.int32)


def _draw(ctx, made, table, slots, eye=EYE, target=TARGET, up=(0.0, 1.0, 0.0)):
    import OpenGL.GL as gl
    projection, view = _matrices(eye, target, up)
    ctx.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    drawn = made._draw_explosions_instanced(projection, view, table, slots, eye)
    glh.no_gl_errors("explosions")
    return drawn, ctx.read_pixels()


def _lit(image):
    return int((glh.luminance(image) > LIT).sum())


def _render(ctx, made, things, brushes=(), play_mode=False):
    import OpenGL.GL as gl
    config = glh.render_config(all_things=list(things), all_brushes=list(brushes),
                               shadows_enabled=False, play_mode=play_mode)
    projection, view = _matrices()
    ctx.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    made.render_scene(projection, view, EYE, None, config,
                      brush_slots=config["all_brush_slots"])
    glh.no_gl_errors("render_scene")
    return ctx.read_pixels(), made.render_stats.draw_calls


def test_the_explosion_program_compiles(renderer):
    _, made = renderer
    assert 'effect_instanced' in made.shaders
    for name in ('uTime', 'uSceneDepth', 'uSoft', 'explosion_texture', 'uFogEnabled'):
        assert made.uniforms['effect_instanced'][name] >= 0, name


def test_the_preview_draws_a_fireball(renderer):
    """The regression: zero lit pixels before the layout fix."""
    ctx, made = renderer
    blank, base_draws = _render(ctx, made, [])
    image, draws = _render(ctx, made, [_explosion(preview=True)])
    assert draws == base_draws + 1
    assert _lit(image) > 500, _lit(image)
    # Centred on the explosion, above its origin (it rises from it).
    rows, cols = np.nonzero(glh.luminance(image) > LIT)
    assert abs(cols.mean() - W / 2) < W * 0.1


def test_a_dormant_explosion_draws_nothing_in_the_editor(renderer):
    ctx, made = renderer
    table, slots = _table([_explosion(preview=False)])
    drawn, image = _draw(ctx, made, table, slots)
    assert drawn == 0 and glh.is_blank(image)


def test_a_triggered_explosion_plays(renderer):
    ctx, made = renderer
    table, slots = _table([_explosion()], runtime=True)
    assert not table.effect_alive[slots[0]]
    table.effect_spawn_time[slots[0]] = time.perf_counter() - 0.2
    table.effect_active[slots[0]] = True
    table.begin_frame(list(table.things[:table.count]), epoch=1, effect_runtime=True)
    assert table.effect_alive[slots[0]]
    drawn, image = _draw(ctx, made, table, slots)
    assert drawn == 1
    assert _lit(image) > 500, _lit(image)


def test_seen_from_above_it_is_a_full_fireball(renderer):
    """An upright card is edge-on from straight above; the fireball faces
    the camera every way, so it is about as big from above as from the side."""
    ctx, made = renderer
    table, slots = _table([_explosion(preview=True)])
    _, side = _draw(ctx, made, table, slots)
    _, above = _draw(ctx, made, table, slots,
                     eye=glm.vec3(0.0, 260.0, -100.0),
                     target=glm.vec3(0.0, 0.0, -100.0), up=(0.0, 0.0, -1.0))
    assert _lit(side) > 500
    assert _lit(above) > 0.5 * _lit(side), (_lit(above), _lit(side))


def test_it_fades_softly_against_a_wall_just_behind_it(renderer, monkeypatch):
    """Against a wall a few units behind, the fireball fades where it meets
    the wall instead of showing the card's hard outline across it."""
    from tests.helpers.worlds import box_brush
    ctx, made = renderer
    wall = box_brush("wall", (0, 40, -116), (600, 400, 16))      # face at z=-108
    things = [_explosion((0.0, -20.0, -100.0), preview=True)]
    wall_only, _ = _render(ctx, made, [], [wall])
    soft, _ = _render(ctx, made, things, [wall])
    # The same frame with no depth copy to read: the hard card.
    monkeypatch.setattr(made, "_capture_scene_depth", lambda: False)
    monkeypatch.setattr(made, "draw_decals", lambda *a, **k: None)
    hard, _ = _render(ctx, made, things, [wall])
    base = glh.luminance(wall_only).astype(np.float64)
    soft_glow = np.abs(glh.luminance(soft) - base).sum()
    hard_glow = np.abs(glh.luminance(hard) - base).sum()
    assert hard_glow > 0
    assert soft_glow < 0.6 * hard_glow, (soft_glow, hard_glow)


def test_explosions_and_particles_share_one_depth_copy(renderer, monkeypatch):
    ctx, made = renderer
    things = [_explosion(preview=True, emitter="SPARKS")]
    table, slots = _table(things)
    projection, view = _matrices()
    calls = []
    real = made._capture_scene_depth
    monkeypatch.setattr(made, "_capture_scene_depth",
                        lambda: calls.append(1) or real())
    ctx.bind()
    made._scene_depth_current = False
    made._draw_explosions_instanced(projection, view, table, slots, EYE)
    made.draw_particles_instanced(projection, view, table, slots,
                                  play_mode=False, camera_pos=EYE)
    assert calls == [1]
    glh.no_gl_errors("explosion + particles")


def test_rows_are_uploaded_only_when_they_change(renderer):
    ctx, made = renderer
    table, slots = _table([_explosion(preview=True)])
    _draw(ctx, made, table, slots)
    first = made._explosion_uploaded
    assert first is not None
    _draw(ctx, made, table, slots)
    assert made._explosion_uploaded is first           # the clock is a uniform
    table.pos[slots[0], 1] += 5.0
    _draw(ctx, made, table, slots)
    assert made._explosion_uploaded is not first


def test_gl_state_is_put_back(renderer):
    import OpenGL.GL as gl
    ctx, made = renderer
    table, slots = _table([_explosion(preview=True)])
    projection, view = _matrices()
    ctx.bind()
    gl.glEnable(gl.GL_CULL_FACE)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glDepthMask(gl.GL_TRUE)
    gl.glDisable(gl.GL_BLEND)
    gl.glBlendFuncSeparate(gl.GL_ONE, gl.GL_ZERO, gl.GL_ONE, gl.GL_ZERO)
    gl.glActiveTexture(gl.GL_TEXTURE3)
    assert made._draw_explosions_instanced(projection, view, table, slots, EYE) == 1
    glh.no_gl_errors("explosions")
    assert gl.glIsEnabled(gl.GL_CULL_FACE)
    assert gl.glIsEnabled(gl.GL_DEPTH_TEST)
    assert not gl.glIsEnabled(gl.GL_BLEND)
    assert bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
    assert [int(gl.glGetIntegerv(name)) for name in (
        gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
        gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA)] == [
        gl.GL_ONE, gl.GL_ZERO, gl.GL_ONE, gl.GL_ZERO]
    assert int(gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)) == gl.GL_TEXTURE3


def test_explosions_write_no_depth(renderer):
    import OpenGL.GL as gl
    ctx, made = renderer
    table, slots = _table([_explosion(preview=True)])
    projection, view = _matrices()
    ctx.bind()
    gl.glClearDepth(1.0)
    gl.glClear(gl.GL_DEPTH_BUFFER_BIT | gl.GL_COLOR_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glDepthMask(gl.GL_TRUE)
    made._draw_explosions_instanced(projection, view, table, slots, EYE)
    depth = gl.glReadPixels(0, 0, W, H, gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT)
    assert np.all(np.asarray(depth, dtype=np.float32) == 1.0)
    assert not glh.is_blank(ctx.read_pixels())
