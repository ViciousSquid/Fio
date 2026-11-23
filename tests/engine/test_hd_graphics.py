"""engine.hd_graphics: which renderer, the PlayerStart's HD sky, exposure.

Plain data and arithmetic -- no OpenGL, no Qt.
"""

import configparser
import math

import numpy as np
import pytest

from engine import hd_graphics as hd


def _config(**renderer):
    config = configparser.ConfigParser()
    config.add_section("Renderer")
    for key, value in renderer.items():
        config.set("Renderer", key, value)
    return config


# -- which renderer ---------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Classic", "Classic"), ("classic", "Classic"), ("Forward", "Classic"),
    ("HD_High", "HD_High"), ("hd_high", "HD_High"), ("hd-high", "HD_High"),
    ("", "Classic"), ("nonsense", "Classic"), (None, "Classic")])
def test_the_renderer_setting_names_classic_or_hd_high(text, expected):
    assert hd.normalise_renderer(text) == expected
    if text is not None:
        assert hd.renderer_from(_config(renderer=text)) == expected


def test_classic_is_the_default_and_low_power_always_gets_it():
    assert hd.renderer_from(None) == "Classic"
    assert hd.renderer_from(_config()) == "Classic"
    assert hd.renderer_from(_config(renderer="HD_High"), lowpower=True) == "Classic"
    assert hd.REGISTRY_NAME == {"Classic": "Forward", "HD_High": "HD_High"}
    assert hd.display_name("Forward") == "Classic" and hd.display_name("HD_High") == "HD_High"


def test_hd_high_is_one_configuration():
    """Two renderers, not a ladder of tiers: one table says what HD_High is."""
    assert set(hd.HD_HIGH) >= {"shadow_size", "reflections", "parallax", "ambient_occlusion",
                               "bloom", "fxaa", "sun_shafts", "cloud_shadows"}
    assert not hasattr(hd, "PRESETS") and not hasattr(hd, "TIERS")


# -- the PlayerStart's properties -------------------------------------------

def test_the_primary_start_wins_and_legacy_values_are_recovered():
    primary = {"hd_sky": "procedural", "hd_sun_elevation": 30.0}
    older = {"hd_sky": "none", "hd_bloom": 0.3}
    newer = {"hd_bloom": 0.9, "hd_grain": 0.05, "hd_tint": ""}
    resolved = hd.resolve_properties(primary, [older, newer])
    assert resolved == {"hd_sky": "procedural", "hd_sun_elevation": 30.0,
                        "hd_bloom": 0.3, "hd_grain": 0.05}
    # Reading changed nothing.
    assert older == {"hd_sky": "none", "hd_bloom": 0.3}
    assert hd.resolve_properties(None, [older]) == older


def test_an_edit_stores_everything_on_the_primary_and_clears_the_others():
    primary = {"name": "start", "hd_sky": "procedural"}
    other = {"name": "other", "hd_bloom": 0.3, "hd_sky": "none", "sky_image": "x.webp"}
    untouched = {"name": "third"}
    changed = hd.canonicalise_properties(primary, [other, untouched])
    assert primary == {"name": "start", "hd_sky": "procedural", "hd_bloom": 0.3}
    assert other == {"name": "other", "sky_image": "x.webp"}      # only hd_* keys go
    assert untouched == {"name": "third"}
    assert [id(c) for c in changed] == [id(primary), id(other)]
    assert hd.canonicalise_properties(primary, [other, untouched]) == []


def test_sky_defaults_follow_the_sky_image():
    assert hd.sky_settings({}, "")["sky"] == "none"
    assert hd.sky_settings({}, "assets/backgrounds/x.webp")["sky"] == "panorama"
    # A panorama sky with no image to be one is no sky.
    assert hd.sky_settings({"hd_sky": "panorama"}, "")["sky"] == "none"
    assert hd.sky_settings({"hd_sky": "procedural"}, "")["sky"] == "procedural"
    assert hd.sky_settings({"hd_sky": "rubbish"}, "")["sky"] == "none"


def test_the_sun_is_on_by_default_only_under_a_procedural_sky():
    assert hd.sky_settings({"hd_sky": "procedural"}, "")["sun"] is True
    assert hd.sky_settings({}, "x.webp")["sun"] is False
    assert hd.sky_settings({"hd_sun": True}, "x.webp")["sun"] is True
    assert hd.sky_settings({"hd_sky": "procedural", "hd_sun": "false"}, "")["sun"] is False
    # Indoors there is no sun whatever is stored.
    assert hd.sky_settings({"hd_sky": "none", "hd_sun": True}, "x.webp")["sun"] is False


def test_fields_default_to_levitates_and_bad_values_are_refused():
    settings = hd.sky_settings({}, "")
    assert settings["sun_azimuth"] == 135.0 and settings["sun_elevation"] == 42.0
    assert settings["sun_intensity"] == 3.4 and settings["turbidity"] == 3.5
    assert settings["exposure"] == 0.0 and settings["bloom"] == 0.10
    assert settings["sun_color"] is None and settings["tint"] == (1.0, 1.0, 1.0)
    bad = hd.sky_settings({"hd_sun_elevation": "steep", "hd_bloom": float("nan"),
                           "hd_contrast": 99, "hd_tint": [255, 128, 0],
                           "hd_sun_color": "#ffffff"}, "")
    assert bad["sun_elevation"] == 42.0 and bad["bloom"] == 0.10
    assert bad["contrast"] == hd.FIELD_BY_KEY["hd_contrast"].high
    assert bad["tint"] == pytest.approx((1.0, (128 / 255) ** 2.2, 0.0))
    assert bad["sun_color"] == pytest.approx((1.0, 1.0, 1.0))


class _Environment:
    pass


@pytest.mark.parametrize("sky", ["none", "panorama", "procedural"])
def test_apply_writes_what_each_sky_needs(sky):
    environment = _Environment()
    settings = hd.sky_settings({"hd_sky": sky, "hd_sun": True}, "sky.webp")
    assert hd.apply(environment, settings) is True
    assert environment.sky_mode == sky and environment.enabled is True
    assert environment.shadow_size == hd.HD_HIGH["shadow_size"]
    if sky == "none":
        # Indoors: no sun, no shadow map, no clouds, no haze.
        assert environment.sun_enabled is False and environment.sun_intensity == 0.0
        assert environment.shadow_strength == 0.0 and environment.haze == 0.0
        assert environment.cloud_cover == 0.0 and environment.sun_shafts == 0.0
    else:
        assert environment.sun_enabled is True and environment.sun_intensity == 3.4
    assert (environment.cloud_cover > 0.0) == (sky == "procedural")
    assert (environment.cloud_shadows > 0.0) == (sky == "procedural")


# -- exposure -----------------------------------------------------------------

def test_the_tone_curve_is_straight_in_the_shadows_and_reaches_white():
    assert hd.tone(0.0) == 0.0
    assert hd.tone(0.01) == pytest.approx(0.0099, abs=2e-4)     # no toe
    assert hd.tone(hd.TONE_WHITE) == pytest.approx(1.0)
    assert hd.tone(hd.KEY_VALUE) == pytest.approx(hd.MID_GREY, abs=1e-6)
    assert all(hd.tone(a) < hd.tone(b) for a, b in zip((0.1, 0.5, 1.0), (0.5, 1.0, 2.0)))


def test_an_indoor_map_is_held_near_classic_brightness():
    classic = hd.classic_exposure()
    dim = hd.base_exposure(hd.scene_light(lamps=[(0.3, 800.0, (1, 1, 1))]), indoor=True)
    bright = hd.base_exposure(hd.scene_light(lamps=[(6.0, 800.0, (1, 1, 1))]), indoor=True)
    assert dim == pytest.approx(classic * hd.INDOOR_LATITUDE)        # not lifted to mid grey
    assert bright == pytest.approx(classic / hd.INDOOR_LATITUDE)
    assert hd.base_exposure(0.0, indoor=True) == classic             # nothing to measure
    # A lamp at full level on a mid-tone comes out at about its own brightness.
    shown = hd.tone(hd.KEY_VALUE * 1.0)       # albedo * gain * exposure at the key
    assert shown == pytest.approx(hd.MID_GREY, abs=1e-6)


def test_under_a_sky_the_exposure_puts_a_typical_surface_on_mid_grey():
    light = hd.scene_light(sky_irradiance=(0.7, 1.1, 1.6), sun_radiance=(3.4, 3.2, 2.9),
                           sun_elevation=42.0)
    exposure = hd.base_exposure(light)
    assert hd.tone(hd.TYPICAL_ALBEDO * light * exposure) == pytest.approx(hd.MID_GREY, abs=1e-6)
    # Brighter light, lower exposure; and it never leaves its range.
    assert hd.base_exposure(light * 4.0) < exposure
    low, high = hd.EXPOSURE_RANGE
    assert hd.base_exposure(1e9) == low and hd.base_exposure(1e-5) == high


def test_exposure_compensation_is_in_stops():
    assert hd.final_exposure(0.5, 0.0) == 0.5
    assert hd.final_exposure(0.5, 1.0) == 1.0
    assert hd.final_exposure(0.5, -2.0) == 0.125


def test_lamps_count_by_the_floor_they_cover_and_ignore_dead_ones():
    one = hd.lamp_light([(1.0, 500.0, (1, 1, 1))])
    assert hd.lamp_light([(1.0, 500.0, (1, 1, 1)), (0.0, 900.0, (1, 1, 1))]) == one
    assert hd.lamp_light([(1.0, 500.0, (1, 1, 1)), (1.0, 5.0, (0, 0, 0))]) == pytest.approx(
        one, rel=1e-3)
    assert hd.lamp_light([]) == 0.0 and hd.lamp_light([(2.0, 500.0, (1, 1, 1))]) > one


def test_a_panoramas_sky_light_is_measured_from_its_pixels():
    height, width = 64, 128
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:height // 2] = (255, 255, 255)        # white above the horizon, black below
    up, side, down, mean = hd.panorama_irradiance(image)
    assert up == pytest.approx((1.0, 1.0, 1.0), abs=0.02)
    assert down == pytest.approx((0.0, 0.0, 0.0), abs=0.02)
    assert side == pytest.approx((0.5, 0.5, 0.5), abs=0.06)
    assert mean == pytest.approx((0.5, 0.5, 0.5), abs=0.03)
    grey = np.full((height, width, 3), 128, dtype=np.uint8)
    assert hd.panorama_irradiance(grey)[0] == pytest.approx(((128 / 255) ** 2.2,) * 3, abs=1e-3)


def test_the_procedural_sky_is_measured_on_the_cpu():
    elevation, azimuth = math.radians(42.0), math.radians(135.0)
    sun = (math.sin(azimuth) * math.cos(elevation), math.sin(elevation),
           math.cos(azimuth) * math.cos(elevation))
    arguments = dict(sun_direction=sun, sun_radiance=(3.4, 3.2, 2.9), turbidity=3.5,
                     rayleigh=1.4, mie=0.005, mie_g=0.8)
    texels = hd.procedural_sky_texels(**arguments)
    assert texels.shape == (8, 16, 3) and np.isfinite(texels).all() and (texels >= 0).all()
    up, _side, down, _mean = hd.irradiance_from_texels(texels)
    assert up[2] > up[0] > 0.1                   # a blue sky, and not a dark one
    assert down == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)      # a black ground
    cloudy = hd.irradiance_from_texels(hd.procedural_sky_texels(cloud_cover=0.9, **arguments))
    assert cloudy[0] != pytest.approx(up)        # clouds change the sky's light


@pytest.mark.qt
def test_classic_colours_survive_the_tone_curve():
    """hdr.display_to_scene is the tone curve's inverse at the base exposure."""
    pytest.importorskip("OpenGL")
    from engine.renderer.hd_high import hdr
    for exposure in (0.3, 1.0, 2.5):
        for colour in ((0.2, 0.5, 0.8), (0.05, 0.05, 0.05), (0.9, 0.3, 0.1)):
            scene = hdr.display_to_scene(colour, exposure)
            shown = [hd.tone(c * exposure) ** (1 / 2.2) for c in scene]
            assert shown == pytest.approx(colour, abs=2e-3)


@pytest.mark.qt
def test_the_generated_shader_keeps_light_loops_below_main():
    """engine.shaders.light_ubo_source writes into main() where it first
    found it, so nothing with a light loop may stand above main."""
    pytest.importorskip("OpenGL")
    from engine.shaders import DEFAULT_SHADERS, light_ubo_source
    from engine.renderer.hd_high import surface
    for name in ("lit.frag", "textured.frag"):
        source = surface.surface_source(DEFAULT_SHADERS[name])
        assert source is not DEFAULT_SHADERS[name], name
        main = source.index("void main()")
        assert "lights[i]" not in source[:main], name
        assert "fioLegacyMain() {" in source[main:] and "void fioShade(" in source[main:]
        final = light_ubo_source(source)
        body = final[final.index("void main()"):]
        assert body.index("fioCookiePixel =") < body.index("fioLegacyMain(); return;"), name
    terrain = surface.terrain_source(DEFAULT_SHADERS["terrain_flat.frag"])
    assert terrain is not None and "lights[i]" not in terrain[:terrain.index("void main()")]
    # A shader of another shape is left alone.
    assert surface.surface_source(DEFAULT_SHADERS["sprite.frag"]) is DEFAULT_SHADERS["sprite.frag"]


def test_hd_highs_small_n_crossovers_are_classics_code():
    """HD_High keeps its own copies of two modules with a small-N crossover
    (it shares no code with the classic renderer). They are the classic
    modules word for word, so the tests that hold each crossover's two paths
    to the same answer hold these too; this fails the day a copy drifts."""
    import os
    from tests.helpers.paths import repo_path
    for name in ("explosion_rows.py", "decals.py"):
        with open(repo_path("engine", "renderer", "forward", name), encoding="utf-8") as handle:
            classic = handle.read()
        with open(repo_path("engine", "renderer", "hd_high", name), encoding="utf-8") as handle:
            copy = handle.read()
        copy = copy.replace("HDHighRenderer", "ForwardRenderer").replace("hd_high", "forward")
        assert copy == classic, name
    del os
