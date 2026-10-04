"""Regression tests for the native top-down player sprite."""

from engine.overhead_sprite import SpriteController


def test_idle_and_walk_frames():
    ctrl = SpriteController(walk_fps=6.0, move_epsilon=0.1)
    ctrl.update((0, 0, 0), 0.0, 0.0, armed=False)
    assert ctrl.frame() == SpriteController.IDLE

    ctrl.update((1, 0, 0), 0.0, 1.0, armed=False)
    assert ctrl.frame() == SpriteController.WALK_A

    ctrl.update((2, 0, 0), 0.0, 1.2, armed=False)
    assert ctrl.frame() == SpriteController.WALK_B


def test_weapon_and_shoot_frames():
    ctrl = SpriteController(move_epsilon=0.1)
    ctrl.update((0, 0, 0), 0.0, 0.0, armed=True)
    assert ctrl.frame() == SpriteController.IDLE_G

    ctrl.update((0, 0, 0), 0.0, 0.01, armed=True, shooting=True)
    assert ctrl.frame() == SpriteController.SHOOT


def test_facing_offset_is_applied():
    from engine.overhead_sprite import OverheadSpriteRenderer
    import math

    renderer = OverheadSpriteRenderer(facing_offset_deg=90.0)
    assert renderer.facing_theta(0.0) == math.pi / 2.0
