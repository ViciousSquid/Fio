"""Real renderer checks for the opaque silhouette and see-through acrylic."""

import glm
import numpy as np
import pytest
import OpenGL.GL as gl

from editor.things import PlayerStart
from tests.helpers import gl as glh

pytestmark = pytest.mark.gl


@pytest.fixture
def scene():
    glh.reset_texture_cache()
    with glh.GLTestContext(256, 256) as context:
        renderer = glh.make_renderer()
        renderer.view_distance.set_ambient_level(.8)
        try:
            yield context, renderer
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def _render(scene, things, play=False):
    context, renderer = scene
    eye = glm.vec3(0, 50, 200)
    projection = glm.ortho(-40., 40., -55., 55., .1, 500.)
    view = glm.lookAt(eye, glm.vec3(0, 50, 0), glm.vec3(0, 1, 0))
    config = glh.render_config(all_things=things, play_mode=play, shadows_enabled=False)
    context.bind()
    gl.glClearColor(.2, .35, .65, 1)
    with glh.no_gl_errors('private playerstart model'):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config['all_brush_slots'])
        gl.glFinish()
    return context.read_pixels()


def _pixel(image, x, y):
    # Orthographic projection above maps X [-40,40], world Y [-5,105].
    return image[round((105-y)*256/110), round((x+40)*256/80)].astype(float)


def test_clear_panel_transmits_background_and_silhouette_remains_opaque(scene):
    image = _render(scene, [PlayerStart(pos=[0, 50, 0], properties={'angle': 270})])
    background = _pixel(image, 35, 60)
    clear_panel = _pixel(image, 18, 60)
    ink = _pixel(image, 0, 60)
    assert 1 < np.linalg.norm(clear_panel - background) < 80
    assert ink.mean() < background.mean() / 3


def test_single_and_instanced_signs_draw_the_same_pixels(scene):
    start = PlayerStart(pos=[0, 50, 0], properties={'angle': 270})
    single = _render(scene, [start])
    # The second sign is outside the camera; it forces the shared mesh batch.
    batch = _render(scene, [start, PlayerStart(pos=[1000, 50, 0])])
    np.testing.assert_allclose(single, batch, atol=2)


def test_editor_model_does_not_leak_into_play(scene):
    empty = _render(scene, [], play=True)
    playing = _render(scene, [PlayerStart(pos=[0, 50, 0])], play=True)
    np.testing.assert_array_equal(empty, playing)


def test_acrylic_draw_restores_blending_and_depth_state(scene):
    context, renderer = scene
    from engine.entity_table import EntityTable
    table = EntityTable()
    table.begin_frame([PlayerStart()], epoch=1)
    context.bind()
    gl.glDisable(gl.GL_BLEND)
    gl.glBlendFunc(gl.GL_ONE, gl.GL_ZERO)
    gl.glDepthMask(gl.GL_TRUE)
    with glh.no_gl_errors('acrylic material state'):
        renderer.draw_models_instanced(glm.mat4(1), glm.mat4(1), glm.vec3(0, 0, 200),
                                       table, np.array([0]), (table, np.array([], dtype=np.int32)))
    assert not gl.glIsEnabled(gl.GL_BLEND)
    assert gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK)
    assert gl.glGetIntegerv(gl.GL_BLEND_SRC_RGB) == gl.GL_ONE
    assert gl.glGetIntegerv(gl.GL_BLEND_DST_RGB) == gl.GL_ZERO
