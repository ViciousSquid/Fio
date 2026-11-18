"""Light cookies, headless: the data path and the shader rewrite.

The visual tier (tests/visual/test_light_cookie_gl.py) checks the pixels;
these check what decides them -- which lights get a cookie id, that the id is
interned once, that the light UBO keeps its size, and that every lighting
shader reads its colour through the cookie and its shadow through the floor.
"""

import re

import numpy as np
import pytest

from engine import light_cookies, shaders
from engine.entity_table import EntityTable
from engine.light_cookies import (NO_COOKIE, cookie_of, cookie_path,
                                  cookie_shadow_floor, intern_cookie)
from engine.package_assets import map_assets

PATH = 'assets/textures/cookies/orientation_test.png'


def test_interning_gives_one_id_per_image():
    first = intern_cookie(PATH)
    assert first > 0
    assert intern_cookie(PATH) == first
    assert intern_cookie('  ' + PATH.replace('/', '\\') + ' ') == first
    assert cookie_path(first) == PATH
    assert intern_cookie('') == NO_COOKIE and intern_cookie(None) == NO_COOKIE
    assert cookie_path(0) == '' and cookie_path(10 ** 6) == ''


def test_a_cookie_is_explicitly_opt_in():
    assert cookie_of({'cookie_texture': PATH}) == NO_COOKIE
    assert cookie_of({'cookie_enabled': False, 'cookie_texture': PATH}) == NO_COOKIE
    assert cookie_of({'cookie_enabled': 'false', 'cookie_texture': PATH}) == NO_COOKIE
    assert cookie_of({'cookie_enabled': True, 'cookie_texture': ''}) == NO_COOKIE
    assert cookie_of({'cookie_enabled': True, 'cookie_texture': PATH}) == intern_cookie(PATH)
    assert cookie_of({'cookie_enabled': 'true', 'cookie_texture': PATH}) == intern_cookie(PATH)


def test_the_shadow_floor_defaults_and_clamps():
    assert cookie_shadow_floor({}) == shaders.COOKIE_SHADOW_FLOOR == 0.15
    assert cookie_shadow_floor({'cookie_shadow_floor': 0.4}) == 0.4
    assert cookie_shadow_floor({'cookie_shadow_floor': -1}) == 0.0
    assert cookie_shadow_floor({'cookie_shadow_floor': 7}) == 1.0
    assert cookie_shadow_floor({'cookie_shadow_floor': 'x'}) == 0.15
    assert cookie_shadow_floor({'cookie_shadow_floor': float('nan')}) == 0.15


@pytest.mark.qt
def test_the_entity_table_carries_the_cookie_and_keeps_shadows_independent():
    from editor.things import Light
    lights = [
        Light(pos=[0, 0, 0]),
        Light(pos=[0, 0, 0], properties={
            'cookie_enabled': True, 'cookie_texture': PATH, 'casts_shadows': True,
            'cookie_shadow_floor': 0.3}),
        Light(pos=[0, 0, 0], properties={'cookie_enabled': False, 'cookie_texture': PATH}),
    ]
    table = EntityTable()
    table.begin_frame(lights, epoch=1)
    assert table.light_cookie[:3].tolist() == [0, intern_cookie(PATH), 0]
    assert table.light_casts_shadows[:3].tolist() == [False, True, False]
    assert np.isclose(table.light_cookie_floor[1], 0.3)


@pytest.mark.qt
def test_the_light_ubo_does_not_grow():
    from engine.renderer.forward.lighting import LightingMixin
    assert LightingMixin.LIGHT_UBO_DTYPE.itemsize == 64


LIT_FRAGMENTS = ('lit.frag', 'textured.frag', 'lit_arm.frag', 'textured_arm.frag',
                 'terrain_flat.frag', 'terrain.frag')


def test_every_lighting_shader_reads_colour_and_shadow_through_the_cookie():
    for name in LIT_FRAGMENTS:
        source = shaders.light_ubo_source(shaders.DEFAULT_SHADERS[name])
        body = source[source.index('void main()'):]
        assert 'fioCookiePixel = max(length(dFdx(' in body, name
        assert re.search(r'fioLightColor\(i, \w+\)', body), name
        assert 'lights[i].color' not in body, name
        assert 'fioCookieShadow(i, calcPointShadow(' in body, name
        assert source.count('uniform mediump sampler2DArray fioCookies;') == 1, name
        # One sample, with an explicit level: no extra taps for the blur.
        assert source.count('textureLod(fioCookies') == 1, name
        assert 'texture(fioCookies' not in source, name


@pytest.mark.qt
def test_the_player_shader_keeps_the_cookie_in_its_packed_block():
    from player.render import world_shaders
    source = world_shaders.FRAGMENT
    assert 'fioLightColor(i, worldPos)' in source
    assert 'lights[i].indices.z' in source
    assert '(float(lights[i].indices.w) * 0.001)' in source
    assert '.params' not in source


@pytest.mark.qt
def test_an_ordinary_frame_loads_nothing():
    """No light with a cookie: one ``any`` over the column, no array."""
    class Fake:
        light_cookie = np.zeros(4, dtype=np.int32)
    from engine.renderer.forward.lighting import LightingMixin
    host = LightingMixin()
    host._light_cookies = None
    assert host._cookie_layers(Fake(), np.arange(4)) is None
    assert host._light_cookies is None


def test_the_cookie_array_skips_unreadable_images():
    # No GL context at all: an image that cannot be read must never reach
    # the upload, so any GL call here would fail on None.
    array = light_cookies.CookieArray(None, read=lambda path: None, size=8)
    missing = intern_cookie('assets/textures/cookies/missing.png')
    assert array.layers([0, missing]).tolist() == [0, 0]
    assert array.texture is None


def test_cookie_images_are_packaged_with_the_map():
    level = {'things': [{'type': 'light', 'properties': {
        'cookie_enabled': True, 'cookie_texture': PATH}}]}
    assert PATH in map_assets(level)
