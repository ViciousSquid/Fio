"""A still frame sends the lights and the shadow filter once, not every frame.

Classic packed its light block and uploaded all 8 KiB of it on every frame,
and sent the shadow filter array to every lit program on every lit pass,
whether or not either had changed. Both are sent when they differ from what
the buffer, or the program, already holds.
"""

import numpy as np
import pytest

import OpenGL.GL as gl

from engine.renderer.core import gl_state
from engine.renderer.forward import lighting
from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.perf]


class Counting:
    """``OpenGL.GL`` with the two calls of interest counted."""

    def __init__(self):
        self.uploads = 0
        self.filters = 0

    def __getattr__(self, name):
        return getattr(gl, name)

    def glBufferData(self, target, *args):
        if target == gl.GL_UNIFORM_BUFFER:
            self.uploads += 1
        return gl.glBufferData(target, *args)

    def glUniform2fv(self, location, count, values):
        if count == lighting.LightingMixin.MAX_SHADOW_LIGHTS:
            self.filters += 1
        return gl.glUniform2fv(location, count, values)


def _frame(context, renderer, config, projection, view, eye):
    context.bind()
    with glh.no_gl_errors("frame"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels()


def test_lights_and_shadow_filter_are_sent_when_they_change(monkeypatch):
    glh.reset_texture_cache()
    with glh.GLTestContext(96, 96) as context:
        renderer = glh.make_renderer(None)
        try:
            brushes, things = glh.lit_cube_scene()
            config = glh.render_config(all_brushes=brushes, all_things=things)
            projection, view, eye = glh.camera_matrices()
            counting = Counting()
            monkeypatch.setattr(lighting, "gl", counting)
            # Uniforms are sent through core.gl_state, on its own ``gl``.
            monkeypatch.setattr(gl_state, "gl", counting)

            first = _frame(context, renderer, config, projection, view, eye)
            assert counting.uploads >= 1 and counting.filters >= 1
            assert not glh.is_blank(first)

            counting.uploads = counting.filters = 0
            still = [_frame(context, renderer, config, projection, view, eye)
                     for _ in range(3)]
            assert counting.uploads == 0, "the light block went up again unchanged"
            assert counting.filters == 0, "the shadow filter was sent again unchanged"
            assert all(np.array_equal(frame, still[0]) for frame in still)

            # A light that moves is a new block: one upload, in that frame.
            table = config["entity_table"]
            light = int(np.flatnonzero(table.light_params[:table.count, 0] > 0)[0])
            table.pos[light, 0] += 40.0
            table.generation += 1
            moved = _frame(context, renderer, config, projection, view, eye)
            assert counting.uploads == 1
            assert not np.array_equal(moved, still[0]), "and the picture follows it"

            counting.uploads = 0
            _frame(context, renderer, config, projection, view, eye)
            assert counting.uploads == 0
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
