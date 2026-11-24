"""Visual tier: the strata walls at the terrain's edge on a real GL context.

Assertions compare renders with each other (walls on or off, more or fewer
layers), never against a particular GPU's shading.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers import terrain_draw

pytestmark = [pytest.mark.gl, pytest.mark.slow]

W, H = 200, 150
SKY = (0.0, 0.0, 1.0)
ENV = {'uFogEnabled': 0, 'uFogColor': (0.5, 0.5, 0.5), 'uFogStart': 1.0e5,
       'uFogEnd': 2.0e5, 'uFogDensity': 0.0, 'uFogCamPos': (0.0, 0.0, 0.0),
       'uAmbient': (0.05, 0.05, 0.05)}


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(W, H) as ctx:
        yield ctx
    glh.reset_texture_cache()


def _terrain(white, **appearance):
    from engine.terrain import Terrain
    t = Terrain(seed=7)
    t.set_bounds(-1, 0, -1, 0)
    t.grass_tex = t.rock_tex = t.sand_tex = t.snow_tex = white
    t.MAX_UPDATES_PER_FRAME = 16
    t.UPDATE_BUDGET_MS = 1.0e6
    if appearance:
        t.set_appearance(**appearance)
    return t


def _render(ctx, terrain, eye, target, frames=3):
    import glm
    import OpenGL.GL as gl
    eye_v = glm.vec3(*eye)
    proj = glm.perspective(glm.radians(60.0), W / H, 1.0, 20000.0)
    view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
    planes = terrain_draw.frustum_planes(proj * view)
    env = dict(ENV, uFogCamPos=tuple(eye))
    held = None
    for _ in range(frames):
        ctx.bind()
        gl.glClearColor(*SKY, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        held = terrain_draw.draw(terrain, proj, view, eye_v, planes, env_uniforms=env)
    assert glh.drain_gl_errors() == []
    return ctx.read_pixels().astype(np.float64), held


def _drawn(image):
    return np.abs(image - np.array(SKY) * 255.0).sum(axis=-1) > 30.0


# Outside the terrain's -x/-z corner, looking back at it from above.
OUTSIDE = ((-900.0, 500.0, -900.0), (0.0, -150.0, 0.0))


def test_the_walls_are_drawn_below_the_edge(context):
    white = glh._white_texture()
    on, held = _render(context, _terrain(white), *OUTSIDE)
    off, _ = _render(context, _terrain(white, strata=False), *OUTSIDE)
    assert held.skirt_sides > 0
    added = _drawn(on) & ~_drawn(off)
    assert added.mean() > 0.05, "the walls added almost nothing below the edge"
    assert not (_drawn(off) & ~_drawn(on)).any(), "the walls hid nothing, only added"


def test_the_layers_change_the_walls(context):
    white = glh._white_texture()
    few, _ = _render(context, _terrain(white, strata_layers=4), *OUTSIDE)
    many, _ = _render(context, _terrain(white, strata_layers=60), *OUTSIDE)
    walls = _drawn(few)
    assert np.abs(few - many).sum(axis=-1)[walls].mean() > 5.0


def test_a_wall_seen_from_behind_is_culled(context):
    """From inside the block, below the walls' foot, every wall is seen from
    behind: none is drawn (a far wall past ground too distant to draw would
    otherwise show its back over the void)."""
    white = glh._white_texture()
    view = ((0.0, -900.0, 0.0), (256.0, -300.0, 30.0))
    on, held = _render(context, _terrain(white), *view)
    off, _ = _render(context, _terrain(white, strata=False), *view)
    assert held.skirt_sides > 0                   # in view, and culled by GL
    assert not (_drawn(on) & ~_drawn(off)).any()
