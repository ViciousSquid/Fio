"""Water reflects the map's sky: the shader contract behind it.

Whenever the map has a sky panorama (the Primary PlayerStart's
``sky_image``), every water brush reflects it where it used to reflect the
procedural sky -- no per-brush setting. Without one, the procedural sky
stays. The texel is worked out per vertex, so a reflecting pixel pays one
texture read and no trigonometry.
"""

from engine import shaders


def test_the_sky_and_the_water_share_one_panorama_mapping():
    # The sky's cube map is drawn from the panorama by the very mapping the
    # water reflects it with.
    cube = shaders.DEFAULT_SHADERS['sky_cube.frag']
    water = shaders.DEFAULT_SHADERS['water.vert']
    assert 'highp vec2 panoramaUV(highp vec3 dir)' in shaders.PANORAMA_GLSL
    assert shaders.PANORAMA_GLSL in cube and 'panoramaUV(' in cube
    # The water works the texel out per vertex, by the same mapping.
    assert shaders.PANORAMA_GLSL in water and 'panoramaUV(reflected)' in water


def test_a_reflecting_pixel_does_no_trigonometry():
    frag = shaders.DEFAULT_SHADERS['water.frag']
    start = frag.index('vec3 skyColor(vec3 dir) {')
    body = frag[start:frag.index('\n}', start)]
    branch = body[body.index('if (reflectSky == 1)'):body.index('}')]
    assert 'textureLod(skyPanorama, SkyUV' in branch
    for trig in ('atan(', 'asin(', 'acos(', 'sin(', 'cos('):
        assert trig not in branch, trig


def test_without_a_sky_the_water_keeps_its_procedural_sky():
    water = shaders.DEFAULT_SHADERS['water.frag']
    for uniform in ('uniform sampler2D skyPanorama;', 'uniform highp int reflectSky;'):
        assert uniform in water
    assert 'vec3(0.66, 0.76, 0.83)' in water and 'SUN_COLOR * (' in water


def test_there_is_no_per_brush_setting_for_it():
    from engine.render_table import RenderTable
    table = RenderTable()
    assert not hasattr(table, 'water_sky_reflection')
    assert not hasattr(table, 'water_reflections')      # the removed planar ones


def test_a_sky_pixel_does_no_trigonometry():
    sky = shaders.DEFAULT_SHADERS['void_background.frag']
    assert 'samplerCube u_sky' in sky
    for trig in ('atan(', 'asin(', 'acos(', 'sin(', 'cos('):
        assert trig not in sky, trig


def test_the_water_shader_never_discards():
    # A shader that can discard loses early depth testing for every draw made
    # with it: the hidden-wall skip writes alpha 0 instead (see water.frag).
    assert 'discard;' not in shaders.DEFAULT_SHADERS['water.frag']
