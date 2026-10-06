"""Water shader contracts.

Planar water reflections (a mirrored-scene render per water surface) were
removed.  The shader keeps its screen-space refraction and sky reflection; the
capture pass, its render targets and the per-brush flag are gone, and these
tests make sure they stay gone rather than being half-reintroduced.
"""

import pytest

from engine import shaders


def test_water_shader_has_glass_style_optical_inputs():
    src = shaders.DEFAULT_SHADERS['water.frag']
    for uniform in (
        'sceneColor',
        'screenSize',
        'distortionStrength',
        'refractionIndex',
        'roughness',
        'fresnelIntensity',
    ):
        assert 'uniform ' in src
        assert uniform in src


def test_water_shader_performs_screen_space_refraction():
    src = shaders.DEFAULT_SHADERS['water.frag']
    assert 'refract(' in src
    assert 'texture(sceneColor, refractUV)' in src
    assert 'screenUV' in src


def test_water_shader_has_no_planar_reflection_inputs():
    src = shaders.DEFAULT_SHADERS['water.frag']
    for removed in ('reflectionTexture', 'reflectionMatrix', 'reflectionEnabled'):
        assert removed not in src, removed


@pytest.mark.gl
def test_the_renderer_has_no_reflection_capture_pass():
    from engine.renderer.forward import ForwardRenderer

    for name in ('WATER_REFLECTION_SIZE', 'WATER_REFLECTION_TEXTURE_UNIT',
                 '_ensure_water_reflection_resources',
                 '_ensure_water_reflection_texture'):
        assert not hasattr(ForwardRenderer, name), name
    assert not hasattr(ForwardRenderer, "_render_water_reflections")
