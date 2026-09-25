"""Water shader and reflection contracts."""

from engine import shaders


def test_water_shader_has_glass_style_optical_inputs():
    src = shaders.DEFAULT_SHADERS['water.frag']
    for uniform in (
        'sceneColor',
        'reflectionCube',
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


def test_water_shader_supports_real_environment_reflections():
    src = shaders.DEFAULT_SHADERS['water.frag']
    assert 'samplerCube reflectionCube' in src
    assert 'reflectionEnabled == 1' in src
    assert 'textureLod(reflectionCube, R, lod)' in src


def test_water_reflection_geometry_is_optional_and_fixed():
    from engine.renderer_core import BaseRenderer

    assert BaseRenderer.WATER_REFLECTION_SIZE == 256
    assert BaseRenderer.WATER_REFLECTION_PROBE_HEIGHT == 256.0
    assert BaseRenderer.WATER_REFLECTION_TEXTURE_UNIT == 3
