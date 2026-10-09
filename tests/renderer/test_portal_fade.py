"""A fading portal changes its opacity: its view over what is behind it.

While a portal fades in or out (Enable / Disable / Toggle), the renderer
draws its view at the fade's opacity over the scene behind the aperture --
not darkened towards black, which then popped to the scene behind at the
end of a fade-out.
"""

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="renderer tests need the production Qt/OpenGL runtime")
pytest.importorskip("OpenGL")

from editor.things import Portal                             # noqa: E402
from engine.entity_table import EntityTable                  # noqa: E402
from tests.helpers import gl as glh                          # noqa: E402
from tests.helpers.worlds import make_thing                  # noqa: E402

pytestmark = pytest.mark.gl

SIZE = 64


@pytest.fixture
def ctx():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        renderer = glh.make_renderer()
        try:
            yield context, renderer
        finally:
            renderer.cleanup()
            glh.reset_texture_cache()


def _scene(fade):
    a = make_thing(Portal, "A", (0.0, 0.0, 0.0), id="portal-a", portal_target="B",
                   width=400.0, height=400.0, show_rim=False)
    b = make_thing(Portal, "B", (2000.0, 0.0, 0.0), id="portal-b", portal_target="A",
                   width=400.0, height=400.0, show_rim=False)
    a._fade_alpha = fade
    table = EntityTable()
    table.begin_frame([a, b], epoch=1)
    return table


def _render(ctx, fade):
    import OpenGL.GL as gl
    context, renderer = ctx
    table = _scene(fade)
    normal = glm.vec3(*(float(v) for v in table.portal_basis[0, 2]))
    eye = glm.vec3(*(float(v) for v in table.pos[0])) + normal * 150.0
    projection = glm.perspective(glm.radians(90.0), 1.0, 1.0, 5000.0)
    view = glm.lookAt(eye, glm.vec3(*(float(v) for v in table.pos[0])), glm.vec3(0, 1, 0))

    context.bind()
    gl.glClearColor(1.0, 0.0, 0.0, 1.0)              # the scene behind: red
    gl.glClearStencil(0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
    gl.glEnable(gl.GL_DEPTH_TEST)

    def portal_view(view_state, cfg):                  # the view through it: blue
        renderer._portal_upload_quad([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)])
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glUseProgram(renderer._portal_rim_shader)
        ident = glm.value_ptr(glm.mat4(1.0))
        gl.glUniformMatrix4fv(renderer._portal_rim_proj_loc, 1, gl.GL_FALSE, ident)
        gl.glUniformMatrix4fv(renderer._portal_rim_view_loc, 1, gl.GL_FALSE, ident)
        gl.glUniform4f(renderer._portal_rim_color_loc, 0.0, 0.0, 1.0, 1.0)
        gl.glBindVertexArray(renderer._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glEnable(gl.GL_DEPTH_TEST)

    renderer.draw_portals(table, table.portal_slots, projection, view, eye,
                          {"play_mode": True, "entity_table": table}, portal_view)
    return context.read_pixels()[SIZE // 2, SIZE // 2].astype(int)


@pytest.mark.parametrize("fade", [1.0, 0.75, 0.25])
def test_the_portal_view_shows_at_the_fades_opacity(ctx, fade):
    pixel = _render(ctx, fade)
    expected = np.array([255 * (1 - fade), 0, 255 * fade])
    assert np.abs(pixel - expected).max() <= 3, (fade, pixel)


def test_a_faded_out_inactive_portal_is_not_drawn(ctx):
    _context, renderer = ctx
    table = _scene(0.0)
    table.portal_active[:] = False
    table.portal_fade[:] = 0.0
    assert not len(renderer._portal_candidate_slots(table, table.portal_slots, glm.vec3(0, 0, 150)))
