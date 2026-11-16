"""Visual tier: the sky panorama is drawn the right way round in every direction.

The sky pass reads a cube map made from the equirectangular panorama when it
loads (``ForwardRenderer._build_sky_cube``). A face built facing the wrong way,
or flipped, would put the wrong part of the sky on screen. So the frame is
compared with the panorama looked up directly by its mapping (``panoramaUV``:
the one the water reflects it with): a synthetic panorama whose colour says
which way it faces -- red and green the compass bearing, blue the height --
seen through views that cross every face, edge and corner of the cube.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 96
PANORAMA = (512, 256)

#: Looking each way along the axes, up, down, and into a cube corner.
VIEWS = ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0),
         (0.3, 1.0, -0.2), (-0.2, -1.0, 0.3), (1.0, 0.7, -1.0), (-1.0, -0.6, 1.0))


def _colour(u, v):
    """The test panorama's colour at panorama coordinates (u, v), 0..1."""
    angle = 2.0 * np.pi * u
    return np.stack([0.5 + 0.5 * np.cos(angle), 0.5 + 0.5 * np.sin(angle), 1.0 - v],
                    axis=-1) * 255.0


def _expected(forward):
    """What a 90-degree square view facing *forward* should show: each
    pixel's ray looked up in the panorama by panoramaUV."""
    f = np.asarray(forward, dtype=np.float64)
    f /= np.linalg.norm(f)
    right = np.cross(f, (0.0, 1.0, 0.0))
    right /= np.linalg.norm(right)
    up = np.cross(right, f)
    ndc = (np.arange(SIZE) + 0.5) / SIZE * 2.0 - 1.0
    x, y = np.meshgrid(ndc, -ndc)                     # top row first, as read
    ray = f + x[..., None] * right + y[..., None] * up
    ray /= np.linalg.norm(ray, axis=-1, keepdims=True)
    u = np.arctan2(ray[..., 0], -ray[..., 2]) / (2.0 * np.pi) + 0.5
    v = 0.5 - np.arcsin(np.clip(ray[..., 1], -1.0, 1.0)) / np.pi
    return _colour(u, v)


def test_every_part_of_the_sky_faces_the_way_it_should(tmp_path):
    import glm
    from PIL import Image

    width, height = PANORAMA
    u = (np.arange(width) + 0.5) / width
    v = (np.arange(height) + 0.5) / height              # row 0 is straight up
    texels = _colour(*np.meshgrid(u, v))
    sky = tmp_path / "compass-panorama.png"
    Image.fromarray(np.round(texels).astype(np.uint8)).save(sky)

    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        renderer = glh.make_renderer()
        try:
            renderer.set_background_image(str(sky))
            projection = glm.perspective(glm.radians(90.0), 1.0, 1.0, 8000.0)
            config = glh.render_config(all_brushes=[], all_things=[], play_mode=True)
            eye = glm.vec3(0.0)
            for forward in VIEWS:
                view = glm.lookAt(eye, glm.vec3(*forward), glm.vec3(0, 1, 0))
                context.bind()
                with glh.no_gl_errors("sky %s" % (forward,)):
                    renderer.render_scene(projection, view, eye, None, config,
                                          brush_slots=config["all_brush_slots"])
                error = np.abs(context.read_pixels().astype(np.float64)
                               - _expected(forward)).max(axis=-1)
                # Texel filtering and 8 bits each way are all that may differ;
                # a face the wrong way round is off by tens to hundreds.
                assert error.mean() < 2.0 and np.percentile(error, 99) < 8.0, (
                    forward, error.mean(), np.percentile(error, 99))
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


@pytest.mark.parametrize("setting, size", [(None, 384), ("512", 512), ("16", 64),
                                           ("huge", 384)])
def test_the_sky_cube_map_is_the_size_settings_ini_asks_for(setting, size):
    """``[Renderer] sky_cubemap_size``, like ``shadow_map_size`` for the
    shadow cube maps: 384 by default, clamped, and a bad value is the default."""
    import configparser
    import OpenGL.GL as gl

    config = configparser.ConfigParser()
    config.add_section('Renderer')
    if setting is not None:
        config.set('Renderer', 'sky_cubemap_size', setting)
    glh.reset_texture_cache()
    with glh.GLTestContext(16, 16):
        renderer = glh.make_renderer(config)
        try:
            renderer.set_background_image("assets/backgrounds/cloudy-sky-panorama.webp")
            assert renderer._background_panorama_texture()
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, renderer._background_cube_texture)
            for face in range(6):
                target = gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face
                assert gl.glGetTexLevelParameteriv(target, 0, gl.GL_TEXTURE_WIDTH) == size
                assert gl.glGetTexLevelParameteriv(target, 0, gl.GL_TEXTURE_HEIGHT) == size
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def test_the_shipped_settings_ask_for_384():
    import configparser
    from tests.helpers.paths import repo_path

    config = configparser.ConfigParser()
    config.read(repo_path('settings.ini'))
    assert config.getint('Renderer', 'sky_cubemap_size') == 384
    assert config.getint('Renderer', 'shadow_map_size') == 384
