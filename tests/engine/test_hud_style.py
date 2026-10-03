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
