"""Visual tier: the two water tiers, on a real OpenGL context.

Quality is per water brush (``water_high_quality``, default on); the
renderer's ``water_quality`` is only a session debug cap ('cheap' forces every
brush cheap). 'cheap' water is the depth-less look. 'expensive' water copies the depth
buffer once per water pass (plain GL 3.3 glCopyTexSubImage2D) for depth
absorption, shoreline foam, caustics and screen-space reflections. Both must
draw without GL errors, and the expensive tier must actually change the frame.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 160


def pool_scene(high_quality=None):
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [
        box_brush('floor', (0, -300, 0), (6000, 64, 6000)),
        box_brush('pillar', (-250, 20, -500), (120, 700, 120)),
    ]
    water = box_brush('water', (0, -130, 0), (5000, 260, 5000))
    water.update(shader='Water', is_water=True, water_tint=[0.05, 0.35, 0.45],
                 water_opacity=0.85, water_fresnel=0.6, water_wave_height=0.25,
                 water_wave_enabled=True)
    if high_quality is not None:
        water['water_high_quality'] = high_quality
    brushes.append(water)
    light = make_thing(Light, 'sun', (0.0, 3000.0, 1500.0), color=[255, 255, 255],
                       intensity=1.0, radius=20000.0, state='on', casts_shadows=False)
    return brushes, [light]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def render(context, quality, high_quality=None):
    import glm
    import OpenGL.GL as gl
    from engine.view_distance import ViewDistance

    renderer = glh.make_renderer()
    try:
        renderer.water_quality = quality
        renderer.view_distance = ViewDistance(8000.0)
        brushes, things = pool_scene(high_quality)
        eye = glm.vec3(-700.0, 160.0, 900.0)
        projection = glm.perspective(glm.radians(65.0), 1.0, 1.0,
                                     renderer.view_distance.far_plane)
        view = glm.lookAt(eye, glm.vec3(100.0, -60.0, -300.0), glm.vec3(0, 1, 0))
        config = glh.render_config(all_brushes=brushes, all_things=things,
                                   all_lights=things, play_mode=True, time=3.0)
        context.bind()
        gl.glClearColor(0.55, 0.7, 0.85, 1.0)
        with glh.no_gl_errors(f"{quality} water"):
            renderer.render_scene(projection, view, eye, brushes, things, None,
                                  config, brush_slots=config["all_brush_slots"])
            gl.glFinish()
        return context.read_pixels().astype(np.float64)
    finally:
        renderer.cleanup()


def test_water_quality_setting_is_normalised():
    from engine.renderer_core import BaseRenderer
    assert BaseRenderer.normalize_water_quality('Cheap ') == 'cheap'
    assert BaseRenderer.normalize_water_quality('EXPENSIVE') == 'expensive'
    assert BaseRenderer.normalize_water_quality('ultra') == 'expensive'


def test_both_water_tiers_draw_and_differ(context):
    cheap = render(context, 'cheap')
    expensive = render(context, 'expensive')
    assert cheap.std() > 0.0 and expensive.std() > 0.0
    # The lower half of the frame is water in both.
    lower = slice(SIZE // 2, SIZE)
    assert np.abs(expensive[lower] - cheap[lower]).mean() > 3.0


def test_cheap_water_makes_no_depth_copy(context):
    import glm
    renderer = glh.make_renderer()
    try:
        renderer.water_quality = 'cheap'
        calls = []
        renderer._capture_scene_depth = lambda: calls.append(1) or True
        from engine.view_distance import ViewDistance
        renderer.view_distance = ViewDistance(8000.0)
        brushes, things = pool_scene()
        eye = glm.vec3(-700.0, 160.0, 900.0)
        projection = glm.perspective(glm.radians(65.0), 1.0, 1.0, 8000.0)
        view = glm.lookAt(eye, glm.vec3(100.0, -60.0, -300.0), glm.vec3(0, 1, 0))
        config = glh.render_config(all_brushes=brushes, all_things=things,
                                   all_lights=things, play_mode=True, time=1.0)
        context.bind()
        renderer.render_scene(projection, view, eye, brushes, things, None,
                              config, brush_slots=config["all_brush_slots"])
        assert calls == []
        renderer.water_quality = 'expensive'
        renderer.render_scene(projection, view, eye, brushes, things, None,
                              config, brush_slots=config["all_brush_slots"])
        assert calls, "expensive water never copied the depth buffer"
    finally:
        renderer.cleanup()


def test_a_brush_with_high_quality_off_draws_the_cheap_look(context):
    capped = render(context, 'cheap')
    brush_cheap = render(context, 'expensive', high_quality=False)
    brush_high = render(context, 'expensive', high_quality=True)
    assert np.abs(brush_cheap - capped).mean() < 0.5
    lower = slice(SIZE // 2, SIZE)
    assert np.abs(brush_high[lower] - brush_cheap[lower]).mean() > 3.0
