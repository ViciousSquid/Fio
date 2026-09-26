"""Water shader and reflection contracts."""

import pytest

from engine import shaders


def test_water_shader_has_glass_style_optical_inputs():
    src = shaders.DEFAULT_SHADERS['water.frag']
    for uniform in (
        'sceneColor',
        'reflectionTexture',
        'reflectionMatrix',
        'reflectionEnabled',
        'screenSize',
        'distortionStrength',
        'refractionIndex',
        'roughness',
        'fresnelIntensity',
    ):
        assert f'uniform ' in src
        assert uniform in src


def test_water_shader_performs_screen_space_refraction():
    src = shaders.DEFAULT_SHADERS['water.frag']
    assert 'refract(' in src
    assert 'texture(sceneColor, refractUV)' in src
    assert 'screenUV' in src


def test_water_shader_supports_planar_reflections():
    src = shaders.DEFAULT_SHADERS['water.frag']
    assert 'sampler2D reflectionTexture' in src
    assert 'uniform mat4 reflectionMatrix' in src
    assert 'reflectionEnabled == 1' in src
    assert 'reflectionMatrix * vec4(FragPos, 1.0)' in src


@pytest.mark.gl
def test_water_reflection_target_is_a_2d_texture():
    from engine.renderer_core import BaseRenderer

    assert BaseRenderer.WATER_REFLECTION_SIZE == 256
    assert not hasattr(BaseRenderer, 'WATER_REFLECTION_PROBE_HEIGHT')
    assert BaseRenderer.WATER_REFLECTION_TEXTURE_UNIT == 3
