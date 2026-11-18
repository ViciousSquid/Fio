"""The lens water pass on a real GL context: drops change the picture where
they are and nowhere else, nothing is drawn with no drops, and GL state is
put back."""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

W, H = 160, 120


@pytest.fixture
def ctx():
    with glh.GLTestContext(W, H) as context:
        yield context


def _stripes():
    import OpenGL.GL as gl
    gl.glEnable(gl.GL_SCISSOR_TEST)
    for i in range(8):
        gl.glScissor(i * W // 8, 0, W // 8, H)
        shade = (i % 2) * 0.9
        gl.glClearColor(shade, 0.2 + 0.08 * i, 1.0 - shade, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT)
    gl.glDisable(gl.GL_SCISSOR_TEST)


def test_a_drop_shows_the_view_turned_over_inside_it(ctx):
    import OpenGL.GL as gl
    from engine.renderer.core.lens_water_gl import LensWaterPass
    pass_ = LensWaterPass()
    ctx.bind()
    _stripes()
    before = ctx.read_pixels().astype(int)
    assert pass_.draw([], (0, 0, W, H)) == 0
    assert np.array_equal(ctx.read_pixels().astype(int), before)

    gl.glEnable(gl.GL_DEPTH_TEST)
    gl.glEnable(gl.GL_BLEND)
    assert pass_.draw([(0.5, 0.5, 0.12, 1.0, 1.0)], (0, 0, W, H)) == 1
    glh.no_gl_errors("lens water")
    after = ctx.read_pixels().astype(int)
    changed = np.abs(after - before).sum(axis=2) > 12
    ys, xs = np.nonzero(changed)
    assert changed.sum() > 200
    # Inside the drop only: around the middle, nowhere near the corners.
    assert abs(xs.mean() - W / 2) < W * 0.1 and abs(ys.mean() - H / 2) < H * 0.12
    assert not changed[:10, :10].any() and not changed[-10:, -10:].any()
    assert gl.glIsEnabled(gl.GL_DEPTH_TEST) and gl.glIsEnabled(gl.GL_BLEND)
    pass_.cleanup()
