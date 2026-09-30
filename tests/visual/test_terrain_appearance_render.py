"""Visual tier: terrain looks and grass on a real OpenGL context.

Compiles the terrain program both ways the engine builds it (the renderer's
light-UBO build and ``Terrain``'s own fallback) and the grass program, then
renders each appearance preset. Assertions compare renders with each other,
never against a particular GPU's shading.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

W, H = 160, 120
SKY = (0.0, 0.0, 1.0)
ENV = {'uFogEnabled': 0, 'uFogColor': (0.5, 0.5, 0.5), 'uFogStart': 1.0e5,
       'uFogEnd': 2.0e5, 'uFogDensity': 0.0, 'uFogCamPos': (0.0, 900.0, 900.0),
       'uAmbient': (0.05, 0.05, 0.05)}


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(W, H) as ctx:
        yield ctx
    glh.reset_texture_cache()


def _terrain(white):
    from engine.terrain import Terrain
    t = Terrain(seed=7)
    t.set_bounds(-1, 0, -1, 0)
    t.grass_tex = t.rock_tex = t.sand_tex = t.snow_tex = white
    t.MAX_UPDATES_PER_FRAME = 16
    return t


def _render(ctx, terrain, eye=(0.0, 380.0, 380.0), target=(0.0, 60.0, -60.0), frames=3):
    import glm
    import OpenGL.GL as gl
    eye_v = glm.vec3(*eye)
    proj = glm.perspective(glm.radians(60.0), W / H, 1.0, 20000.0)
    view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
    env = dict(ENV, uFogCamPos=tuple(eye))
    for _ in range(frames):
        ctx.bind()
        gl.glClearColor(*SKY, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        terrain.update_and_render(proj, view, eye_v, None, None, 0, env_uniforms=env)
    assert glh.drain_gl_errors() == []
    return ctx.read_pixels().astype(np.float64)


def _ground(image):
    """Pixels that are not the clear colour."""
    sky = np.array(SKY) * 255.0
    return np.abs(image - sky).sum(axis=-1) > 30.0


def test_terrain_program_links_with_the_light_block(context):
    renderer = glh.make_renderer()
    try:
        assert renderer.shaders.get('terrain')
    finally:
        renderer.cleanup()


def test_every_look_renders_and_differs(context):
    white = glh._white_texture()
    images = {}
    from engine import terrain_style as ts
    for name in ts.PRESETS:
        t = _terrain(white)
        assert t.shader_program, "terrain fallback program failed to link"
        t.apply_appearance_preset(name)
        images[name] = _render(context, t)
        assert _ground(images[name]).mean() > 0.2, name
    names = list(images)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            diff = np.abs(images[a] - images[b]).mean()
            assert diff > 3.0, f"{a} and {b} render the same"


def test_textured_terrain_blends_height_layers(context):
    """With textures on, each layer's texture shows where its layer is."""
    import OpenGL.GL as gl

    def solid(rgb):
        tex = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0, gl.GL_RGBA,
                        gl.GL_UNSIGNED_BYTE, bytes(list(rgb) + [255]))
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
        return tex

    t = _terrain(glh._white_texture())
    t.grass_tex = solid((0, 255, 0))
    t.rock_tex = solid((255, 0, 0))
    t.sand_tex = solid((255, 255, 0))
    t.snow_tex = solid((255, 255, 255))
    t.use_textures = True
    t.set_appearance(layer_heights=(0.25, 0.5, 0.75), slope_rock=0.0)
    image = _render(context, t)
    ground = image[_ground(image)]
    r, g, b = ground[:, 0], ground[:, 1], ground[:, 2]
    greenish = (g > r * 1.3) & (g > b * 1.3)
    reddish = (r > g * 1.3) & (r > b * 1.3)
    whitish = (b > 0.6 * r) & (b > 0.6 * g) & (r > 120)
    # Several layers are visible at once, not a single texture.
    assert greenish.mean() > 0.05
    assert reddish.mean() > 0.05
    assert whitish.mean() > 0.01


def test_grass_draws_in_both_detail_levels(context):
    t = _terrain(glh._white_texture())
    assert t.grass_shader_program, "grass program failed to link"
    bare = _render(context, t, eye=(40.0, 0.0, 120.0), target=(40.0, 0.0, 0.0))
    t.set_grass(True, density=0.06)
    t.set_appearance(layer_heights=(0.0, 1.0, 1.0))
    t.GRASS_MIN_NORMAL_Y = 0.0
    y = t._get_height_scalar(40.0, 80.0)
    eye = (40.0, y + 12.0, 120.0)
    target = (40.0, t._get_height_scalar(40.0, 0.0), 0.0)
    bare = _render(context, _terrain(glh._white_texture()), eye=eye, target=target)
    near = _render(context, t, eye=eye, target=target)
    assert np.abs(near - bare).mean() > 2.0

    # Far blades: push the detail distance in so every chunk draws the
    # single-triangle blade.
    t.GRASS_LOD_START, t.GRASS_LOD_DISTANCE = 1.0, 2.0
    far = _render(context, t, eye=eye, target=target)
    assert np.abs(far - bare).mean() > 2.0
