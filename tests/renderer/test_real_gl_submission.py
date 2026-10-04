"""Real renderer submission tests.

These are deliberately not table/unit tests. They create a real OpenGL 3.3
context, build the production RenderTable/EntityTable projections, run the
production forward renderer, and prove that both a brush and a sprite reach
the driver as raster work.
"""

import numpy as np
import pytest

from editor.things import Thing
from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing

pytestmark = pytest.mark.gl


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(128, 128) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    renderer = glh.make_renderer()
    try:
        yield renderer
    finally:
        renderer.cleanup()


def _draw(renderer, context, brushes, things):
    import OpenGL.GL as gl

    projection, view, eye = glh.camera_matrices(
        aspect=1.0,
        eye=(0.0, 100.0, 420.0),
        target=(0.0, 70.0, 0.0),
    )
    config = glh.render_config(
        all_brushes=brushes,
        all_things=things,
        play_mode=True,
        show_sprites_in_play_mode=True,
        camera_distance_cull=False,
        brush_display_mode="Textured",
        shadows_enabled=False,
    )

    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)

    query = gl.glGenQueries(1)[0]
    gl.glBeginQuery(gl.GL_SAMPLES_PASSED, query)
    renderer.render_scene(
        projection,
        view,
        eye,
        None,
        None,
        None,
        config,
        clear=False,
        brush_slots=config["all_brush_slots"],
    )
    gl.glEndQuery(gl.GL_SAMPLES_PASSED)
    gl.glFinish()
    samples = int(gl.glGetQueryObjectuiv(query, gl.GL_QUERY_RESULT))
    gl.glDeleteQueries(1, [query])

    errors = glh.drain_gl_errors()
    assert not errors, "real renderer submission raised GL errors: %s" % errors
    return context.read_pixels(), samples, config


def test_real_brush_is_rasterized_by_forward_renderer(renderer, context):
    brush = box_brush("test_cube", (0.0, 50.0, 0.0), (180.0, 100.0, 180.0))

    image, samples, config = _draw(renderer, context, [brush], [])

    assert config["render_table"].count == 1
    assert config["all_brush_slots"].tolist() == [0]
    assert samples > 0, "the brush frame submitted no raster samples"
    assert not glh.is_blank(image), "the brush never reached the GL framebuffer"


def test_real_sprite_is_rasterized_by_forward_renderer(renderer, context):
    sprite = make_thing(
        Thing,
        "test_sprite",
        (0.0, 90.0, 120.0),
        render_mode="billboard",
        sprite_path="assets/sprites/test.png",
        sprite_size=[96.0, 96.0],
    )

    image, samples, config = _draw(renderer, context, [], [sprite])

    table = config["entity_table"]
    assert table.count == 1
    assert table.sprite_key_id[0] >= 0
    assert config["visible_thing_slots"].tolist() == [0]
    assert samples > 0, "the sprite frame submitted no raster samples"
    assert not glh.is_blank(image), "the sprite never reached the GL framebuffer"


def test_real_brush_and_sprite_share_one_gl_frame(renderer, context):
    brush = box_brush("backdrop", (0.0, 40.0, 0.0), (320.0, 80.0, 320.0))
    sprite = make_thing(
        Thing,
        "test_sprite",
        (0.0, 100.0, 100.0),
        render_mode="billboard",
        sprite_path="assets/sprites/test.png",
        sprite_size=[96.0, 96.0],
    )

    image, samples, config = _draw(renderer, context, [brush], [sprite])

    assert config["render_table"].count == 1
    assert config["entity_table"].count == 1
    assert config["entity_table"].sprite_key_id[0] >= 0
    assert samples > 0
    assert not glh.is_blank(image)
    assert np.count_nonzero(glh.luminance(image)) > 0
