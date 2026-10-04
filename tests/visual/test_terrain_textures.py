"""Visual tier: "use textures" shows the terrain textures.

With ``use_textures`` on, the terrain went darker and still looked untextured:

* the splat textures were multiplied by the biome vertex colour as well --
  green grass times green vertex colour roughly squares the darkness, and the
  vertex colour's per-chunk seams came along with it;
* they repeated every 20 world units, half a player-height, so at any ordinary
  viewing distance they were minified to their average colour.

These tests pin the fix against the real textures in assets/textures/terrain.
"""

import json
import os

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(320, 320) as ctx:
        yield ctx
    glh.reset_texture_cache()


def map_terrain(textured):
    from engine.terrain import Terrain
    t = Terrain()
    with open(os.path.join(ROOT, "maps", "BigWorld_streaming_test.json")) as f:
        t.from_dict(json.load(f)["terrain_data"])
    t.use_textures = textured
    return t


def render(context, t, eye, target, up=(0.0, 1.0, 0.0), fov=70.0):
    import glm
    import OpenGL.GL as gl
    from engine.view_distance import ViewDistance
    renderer = glh.make_renderer()
    renderer.view_distance = ViewDistance(3000.0)
    renderer.setup_terrain_shader(t)
    eye_v = glm.vec3(*eye)
    projection = glm.perspective(glm.radians(fov), 1.0, 1.0, 3000.0)
    view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(*up))
    config = glh.render_config(all_brushes=[], all_things=[], terrain=t,
                               shadows_enabled=False)
    t.MAX_UPDATES_PER_FRAME = 100000
    t.UPDATE_BUDGET_MS = 1e9
    for _ in range(2):
        context.bind()
        gl.glClearColor(0.1, 0.1, 0.15, 1.0)
        renderer.render_scene(projection, view, eye_v, None, config,
                              brush_slots=config["all_brush_slots"])
        gl.glFinish()
    image = context.read_pixels()[:, :, :3].astype(float)
    try:
        renderer.cleanup()
    except Exception:
        pass
    return image


def player_view(t):
    x, z = 300.0, 300.0
    y = t.get_height_at(x, z) + 40.0          # the player's eye height
    return (x, y, z), (x - 200.0, y - 25.0, z - 400.0)


def luma(pixels):
    return pixels @ np.array([0.299, 0.587, 0.114])


def detail(pixels):
    """Mean absolute Laplacian of the luma: texture detail, not shading."""
    y = luma(pixels)
    lap = (4 * y[1:-1, 1:-1] - y[:-2, 1:-1] - y[2:, 1:-1] - y[1:-1, :-2] - y[1:-1, 2:])
    return float(np.abs(lap).mean())


def test_the_terrain_textures_are_visible_and_not_darker(context):
    plain = map_terrain(False)
    textured = map_terrain(True)
    eye, target = player_view(plain)
    a = render(context, plain, eye, target)[160:]     # the ground half
    b = render(context, textured, eye, target)[160:]
    assert detail(b) > 3 * detail(a), (
        f"textured ground shows no texture detail ({detail(b):.2f} vs "
        f"{detail(a):.2f} untextured)")
    assert luma(b).mean() >= luma(a).mean(), (
        f"textured ground is darker ({luma(b).mean():.1f} vs {luma(a).mean():.1f})")
    real = [os.path.join(ROOT, "assets", "textures", "terrain", n)
            for n in ("grass.jpg", "rock.jpg", "sand.jpg", "snow.jpg")]
    assert all(os.path.exists(p) for p in real)
    assert all(getattr(textured, a) for a in ("grass_tex", "rock_tex", "sand_tex", "snow_tex"))


def solid_texture(rgb):
    import OpenGL.GL as gl
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 1, 1, 0, gl.GL_RGBA,
                    gl.GL_UNSIGNED_BYTE, bytes([*rgb, 255]))
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    return tex


def test_the_texture_is_the_surface_colour(context):
    """Magenta textures give magenta ground -- not magenta times biome green."""
    from engine.terrain import BiomeConfig
    t = map_terrain(True)
    # An all-green biome gradient: multiplying the texture by the vertex
    # colour, as the old shading did, would turn magenta nearly black.
    t.biome = BiomeConfig(name="green", color_gradient=[
        (0.0, (0.0, 0.8, 0.0)), (1.0, (0.0, 0.8, 0.0))])
    magenta = solid_texture((230, 40, 230))
    t.grass_tex = t.rock_tex = t.sand_tex = t.snow_tex = magenta
    eye, target = player_view(t)
    ground = render(context, t, eye, target)[160:].reshape(-1, 3)
    r, g, b = ground.mean(axis=0)
    assert r > 1.8 * g and b > 1.8 * g, f"ground is not magenta: {r:.1f} {g:.1f} {b:.1f}"


def border_step_ratio(image):
    """How much the centre row/column's step stands out from a typical one."""
    col = np.abs(np.diff(image, axis=1)).mean(axis=(0, 2))
    row = np.abs(np.diff(image, axis=0)).mean(axis=(1, 2))
    c = image.shape[1] // 2 - 1
    r = image.shape[0] // 2 - 1
    return col[c] / np.median(col), row[r] / np.median(row)


def test_textured_terrain_shows_no_chunk_seams(context):
    """Looking straight down on the corner where four chunks meet."""
    views = {}
    for textured in (False, True):
        t = map_terrain(textured)
        views[textured] = render(context, t, eye=(257.0, 600.0, 256.0),
                                 target=(256.0, 0.0, 256.0), up=(0.0, 0.0, -1.0),
                                 fov=50.0)
    plain_x, plain_z = border_step_ratio(views[False])
    tex_x, tex_z = border_step_ratio(views[True])
    # Untextured still has the per-chunk colour seam (B2, not fixed yet): the
    # test would be meaningless if the corner were not on a chunk border.
    assert plain_x > 5 and plain_z > 5, (plain_x, plain_z)
    assert tex_x < 2 and tex_z < 2, f"textured chunk seam: {tex_x:.2f}, {tex_z:.2f}"
