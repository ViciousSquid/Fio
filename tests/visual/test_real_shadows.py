"""Real shadowing tests: authored geometry -> dense tables -> GL -> screen readback."""

import random

import numpy as np
import pytest

from engine.constants import RENDER_MODE_LIT
from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing

pytestmark = [pytest.mark.gl, pytest.mark.slow]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(192, 192) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    made = glh.make_renderer()
    try:
        yield made
    finally:
        made.cleanup()


def _project(brushes, things, epoch, shadows=True):
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable

    table = RenderTable()
    table.begin_frame(brushes, epoch=epoch)
    render_refs = np.empty(len(brushes), dtype=object)
    if brushes:
        render_refs[:] = brushes

    etable = EntityTable()
    hidden = etable.begin_frame(things, epoch=epoch)
    entity_refs = np.empty(len(things), dtype=object)
    if things:
        entity_refs[:] = things

    return {
        "render_table": table,
        "render_refs": render_refs,
        "all_brush_slots": np.arange(table.count, dtype=np.int32),
        "entity_table": etable,
        "entity_refs": entity_refs,
        "visible_thing_slots": np.arange(etable.count, dtype=np.int32),
        "thing_hidden": hidden,
        "render_mode": RENDER_MODE_LIT,
        "brush_display_mode": "Textured",
        "play_mode": True,
        "camera_distance_cull": False,
        "shadows_enabled": bool(shadows),
        "grid_visible": False,
    }


def _draw(renderer, context, brushes, things, epoch, eye, target, shadows=True):
    import OpenGL.GL as gl

    projection, view, eye_vec = glh.camera_matrices(
        aspect=1.0, eye=eye, target=target
    )
    config = _project(brushes, things, epoch, shadows=shadows)

    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(
        gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT
    )

    renderer.render_scene(
        projection,
        view,
        eye_vec,
        None,
        config,
        brush_slots=config["all_brush_slots"],
    )
    gl.glFinish()

    errors = glh.drain_gl_errors()
    assert not errors, "real shadow render raised GL errors: %s" % errors

    # Read the actual framebuffer. Internal render counters cannot satisfy this:
    # the authored objects must visibly reach the screen.
    image = context.read_pixels()
    assert not glh.is_blank(image), "the real geometry did not appear on screen"
    return image


def _moving_light_scene():
    from editor.things import Light

    brushes = [
        box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
        box_brush("pillar_a", (-180, 80, 0), (96, 192, 96)),
        box_brush("pillar_b", (0, 128, 80), (128, 288, 128)),
        box_brush("pillar_c", (180, 64, -40), (112, 160, 112)),
    ]
    light = make_thing(
        Light,
        "moving_shadow_light",
        (-300, 360, 220),
        color=[255, 255, 255],
        intensity=2.5,
        radius=1400.0,
        state="on",
        casts_shadows=True,
    )
    return brushes, light
