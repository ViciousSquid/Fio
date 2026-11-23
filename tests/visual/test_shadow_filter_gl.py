"""Visual tier: a shadow-casting Light's own filter and shadow-map size.

A Light's ``shadow_map_size`` (0: [Renderer] shadow_map_size) sizes its cube
map, and only a slot a shadow-casting light takes is sized at all; its
``shadow_softness`` (the PCF disk radius in shadow-map texels) and
``shadow_bias`` (the multiplier on the acne bias) reach every lit pass as
that slot's ``uShadowFilter`` values.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    made = glh.make_renderer()
    yield made
    try:
        made.cleanup()
    except Exception:
        pass


def _scene(**light_props):
    brushes, things = glh.lit_cube_scene(shadows=True)
    things[0].properties.update(light_props)
    return brushes, things


def _render(renderer, context, brushes, things, frames=2):
    import OpenGL.GL as gl
    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things)
    image = None
    for _ in range(frames):
        context.bind()
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
        gl.glFinish()
        image = context.read_pixels()
    return image, config


def _cube_width(texture):
    import OpenGL.GL as gl
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, texture)
    width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X, 0,
                                        gl.GL_TEXTURE_WIDTH)
    gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
    return int(width)


def _light_slot(renderer):
    assert len(renderer._light_shadow_index) == 1, renderer._light_shadow_index
    return next(iter(renderer._light_shadow_index.values()))


@pytest.mark.parametrize("authored, expected", [(0, None), (256, 256), (512, 512)])
def test_a_lights_shadow_map_size_sizes_its_cube_map(renderer, context, authored, expected):
    brushes, things = _scene(shadow_map_size=authored)
    with glh.no_gl_errors("rendering a sized shadow"):
        _render(renderer, context, brushes, things)
    slot = _light_slot(renderer)
    expected = renderer.shadow_map_size if expected is None else expected
    assert _cube_width(renderer._shadow_cubemaps[slot]) == expected
    # Every slot no shadow-casting light took is still the 1x1 placeholder.
    for other, texture in enumerate(renderer._shadow_cubemaps):
        if other != slot:
            assert _cube_width(texture) == 1, other


def test_changing_a_lights_shadow_map_size_resizes_and_redraws_it(renderer, context):
    brushes, things = _scene(shadow_map_size=256)
    _render(renderer, context, brushes, things)
    slot = _light_slot(renderer)
    assert _cube_width(renderer._shadow_cubemaps[slot]) == 256
    things[0].properties['shadow_map_size'] = 512
    _render(renderer, context, brushes, things)
    assert _cube_width(renderer._shadow_cubemaps[slot]) == 512


def test_the_lights_filter_reaches_the_lit_passes(renderer, context):
    brushes, things = _scene(shadow_softness=3.5, shadow_bias=2.0)
    _render(renderer, context, brushes, things)
    slot = _light_slot(renderer)
    assert tuple(renderer._shadow_filter[slot]) == pytest.approx((3.5, 2.0))
    others = [s for s in range(renderer.MAX_SHADOW_LIGHTS) if s != slot]
    assert not renderer._shadow_filter[others].any()


def _edge_sharpness(image):
    lum = glh.luminance(image).astype(np.float64)
    return np.abs(np.diff(lum, axis=0)).mean() + np.abs(np.diff(lum, axis=1)).mean()


def test_a_lower_softness_draws_a_sharper_shadow_edge(renderer, context):
    brushes, things = _scene(shadow_softness=0.25)
    sharp, _ = _render(renderer, context, brushes, things)
    things[0].properties['shadow_softness'] = 6.0
    soft, _ = _render(renderer, context, brushes, things)
    # Same scene and camera: only the shadow's penumbra differs.
    assert not np.array_equal(sharp, soft), "softness changed nothing"
    assert _edge_sharpness(sharp) > _edge_sharpness(soft)
