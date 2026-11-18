"""Visual tier: water "Direction". A flowing brush's surface moves; a still
brush -- in the same pass, drawn after a flowing one -- is drawn exactly as
it is without any flowing water in the map."""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192


def _scene(flow_left=None):
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [box_brush('floor', (0, -300, 0), (6000, 64, 6000))]
    for name, x, flow in (('left', -700.0, flow_left), ('right', 700.0, None)):
        water = box_brush(name, (x, -130, 0), (1100, 260, 1100))
        water.update(shader='Water', is_water=True, water_tint=[0.05, 0.35, 0.45],
                     water_opacity=0.85, water_fresnel=0.6, water_wave_height=0.25,
                     water_wave_enabled=True, water_plane=True)
        if flow:
            water['water_flow'] = flow
        brushes.append(water)
    light = make_thing(Light, 'sun', (0.0, 3000.0, 0.0), color=[255, 255, 255],
                       intensity=1.0, radius=20000.0, state='on', casts_shadows=False)
    return brushes, [light]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def _render(context, flow_left=None, time=3.0):
    import glm
    import OpenGL.GL as gl
    from engine.view_distance import ViewDistance

    renderer = glh.make_renderer()
    renderer.view_distance = ViewDistance(8000.0)
    brushes, things = _scene(flow_left)
    eye = glm.vec3(0.0, 1900.0, 1.0)                     # straight down on both pools
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0,
                                 renderer.view_distance.far_plane)
    view = glm.lookAt(eye, glm.vec3(0.0, -130.0, 0.0), glm.vec3(0, 0, -1))
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               all_lights=things, play_mode=True, time=time)
    context.bind()
    gl.glClearColor(0.55, 0.7, 0.85, 1.0)
    with glh.no_gl_errors("water flow"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
        gl.glFinish()
    return context.read_pixels().astype(np.int16)


def _changed(a, b):
    return np.abs(a - b).sum(axis=-1) > 0


def test_flowing_water_moves_and_still_water_beside_it_does_not(context):
    still = _render(context)
    flowing = _render(context, flow_left='E')
    changed = _changed(still, flowing)
    half = SIZE // 2
    assert changed[:, :half].mean() > 0.2                  # the flowing pool moved
    assert not changed[:, half + 8:].any()                 # the still one: untouched


def test_each_direction_flows_its_own_way(context):
    frames = {d: _render(context, flow_left=d) for d in 'NSEW'}
    half = SIZE // 2
    for a in 'NSEW':
        for b in 'NSEW':
            if a < b:
                assert _changed(frames[a], frames[b])[:, :half].any(), (a, b)
