"""Focused tests for HUD health text sizing."""

import pytest

pytest.importorskip("PyQt5")

from engine.qt_game_view import QtGameView


def test_hud_style_2_health_text_is_smaller_than_default():
    for height in (480, 600, 720, 1080, 1440):
        style_1 = QtGameView._hud_health_point_size(1, height)
        style_2 = QtGameView._hud_health_point_size(2, height)

        assert style_2 < style_1


def test_hud_health_point_sizes_keep_other_styles_unchanged():
    assert QtGameView._hud_health_point_size(3, 720) == 24
    assert QtGameView._hud_health_point_size(1, 720) == max(
        42, min(68, int(720 * 0.085))
    )


def test_hud_style_2_health_text_uses_compact_bounds():
    assert QtGameView._hud_health_point_size(2, 1) == 32
    assert QtGameView._hud_health_point_size(2, 10_000) == 52


def test_hud_style_3_uses_a_white_spaced_ammo_counter():
    import inspect

    source = inspect.getsource(QtGameView._draw_hud)

    assert 'if style == 3:' in source
    assert 'ammo_x += 2 * metrics.horizontalAdvance(" ")' in source
    assert 'Qt.white if style == 3 else self._hud_ammo_green' in source


# -- ammo and armor ----------------------------------------------------------

from engine.items import DEFAULT_REGISTRY  # noqa: E402

PISTOL = DEFAULT_REGISTRY.resolve("gun1").weapon
SHOTGUN = DEFAULT_REGISTRY.resolve("gun2").weapon
CIGARETTE = DEFAULT_REGISTRY.resolve("custom1").weapon


@pytest.mark.parametrize("style", [1, 4])
def test_armor_sits_underneath_ammo(style):
    assert QtGameView._hud_stat_lines(style, SHOTGUN, 7, 40) == ["7", "40"]


@pytest.mark.parametrize("style", [2, 3])
def test_styles_2_and_3_put_armor_along_the_bottom_two_spaces_on(style):
    assert QtGameView._hud_stat_lines(style, SHOTGUN, 7, 40) == ["7  40"]


def test_a_weapon_that_spends_no_ammo_shows_infinity():
    assert QtGameView._hud_stat_lines(1, PISTOL, 0, 0) == ["∞"]


def test_no_armor_and_no_firing_weapon_show_nothing():
    assert QtGameView._hud_stat_lines(1, CIGARETTE, 5, 0) == []
    assert QtGameView._hud_stat_lines(2, None, 5, 0) == []


def test_armor_shows_without_a_weapon():
    assert QtGameView._hud_stat_lines(1, None, 0, 25) == ["25"]
    assert QtGameView._hud_stat_lines(1, CIGARETTE, 0, 25) == ["25"]


def test_the_weapon_switch_flash_fades_in_holds_and_fades_out_quickly():
    alpha = QtGameView._weapon_switch_alpha
    total = (QtGameView.WEAPON_SWITCH_FADE_IN + QtGameView.WEAPON_SWITCH_HOLD
             + QtGameView.WEAPON_SWITCH_FADE_OUT)
    assert total < 1.0
    assert alpha(-0.01) == 0.0
    assert alpha(0.0) == 0.0
    assert 0.0 < alpha(QtGameView.WEAPON_SWITCH_FADE_IN / 2) < 1.0
    assert alpha(QtGameView.WEAPON_SWITCH_FADE_IN + 0.01) == 1.0
    fading = QtGameView.WEAPON_SWITCH_FADE_IN + QtGameView.WEAPON_SWITCH_HOLD + 0.05
    assert 0.0 < alpha(fading) < 1.0
    assert alpha(total + 1e-6) == 0.0
    assert alpha(total + 1.0) == 0.0
