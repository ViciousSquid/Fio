"""Tests for the pure-logic input model (state, touch, gamepad)."""

import unittest

from player.input.state import InputState, ButtonEdge, ACTION_JUMP, ACTION_FIRE
from player.input.touch import TouchControls
from player.input.gamepad import GamepadSnapshot, GamepadMapping, apply_gamepad


class TestInputState(unittest.TestCase):
    def test_button_edges(self):
        st = InputState()
        st.begin_frame()
        st.set_button(ACTION_JUMP, True)
        self.assertEqual(st.edge(ACTION_JUMP), ButtonEdge.PRESSED)
        self.assertTrue(st.just_pressed(ACTION_JUMP))

        st.begin_frame()
        st.set_button(ACTION_JUMP, True)
        self.assertEqual(st.edge(ACTION_JUMP), ButtonEdge.HELD)

        st.begin_frame()  # not set this frame
        self.assertEqual(st.edge(ACTION_JUMP), ButtonEdge.RELEASED)
        self.assertTrue(st.just_released(ACTION_JUMP))

        st.begin_frame()
        self.assertEqual(st.edge(ACTION_JUMP), ButtonEdge.UP)

    def test_axis_clamped_and_accumulated(self):
        st = InputState()
        st.begin_frame()
        st.add_move(0.8, 0.0)
        st.add_move(0.8, 0.0)  # two sources -> clamp to 1.0
        self.assertEqual(st.move_x, 1.0)

    def test_button_or_across_sources(self):
        st = InputState()
        st.begin_frame()
        st.set_button(ACTION_FIRE, False)
        st.set_button(ACTION_FIRE, True)  # second source holds it
        self.assertTrue(st.is_down(ACTION_FIRE))


class TestTouchControls(unittest.TestCase):
    W, H = 1000.0, 500.0

    def test_move_stick_grabs_and_reports(self):
        tc = TouchControls(dead_zone=0.0)
        st = InputState()
        st.begin_frame()
        # Finger lands in left region -> becomes stick origin.
        tc.apply(st, {1: (200.0, 250.0)}, self.W, self.H)
        # Same position => zero movement.
        self.assertAlmostEqual(st.move_x, 0.0, places=5)

        st.begin_frame()
        # Drag right and up (screen y smaller). radius = 0.16 * min(W,H)=80px.
        tc.apply(st, {1: (280.0, 170.0)}, self.W, self.H)
        self.assertGreater(st.move_x, 0.9)   # +80px / 80px ~ 1.0 strafe right
        self.assertGreater(st.move_y, 0.9)   # up on screen -> forward

    def test_button_hit(self):
        tc = TouchControls()
        st = InputState()
        st.begin_frame()
        # FIRE button centre is (0.90*W, 0.82*H) = (900, 410).
        tc.apply(st, {5: (900.0, 410.0)}, self.W, self.H)
        self.assertTrue(st.is_down(ACTION_FIRE))

    def test_look_drag_turns(self):
        tc = TouchControls()
        st = InputState()
        st.begin_frame()
        # Land on the right side (not over a button) -> look pointer.
        tc.apply(st, {9: (600.0, 200.0)}, self.W, self.H)
        st.begin_frame()
        tc.apply(st, {9: (660.0, 200.0)}, self.W, self.H)  # drag right
        self.assertGreater(st.look_x, 0.0)

    def test_lift_releases_move_origin(self):
        tc = TouchControls()
        st = InputState()
        st.begin_frame()
        tc.apply(st, {1: (200.0, 250.0)}, self.W, self.H)
        self.assertIsNotNone(tc.move_stick.origin)
        st.begin_frame()
        tc.apply(st, {}, self.W, self.H)  # finger lifted
        self.assertIsNone(tc.move_stick.origin)


class TestGamepad(unittest.TestCase):
    def test_stick_dead_zone(self):
        st = InputState()
        st.begin_frame()
        snap = GamepadSnapshot(left_x=0.1, left_y=0.0)  # inside dead zone
        apply_gamepad(st, snap, GamepadMapping())
        self.assertEqual(st.move_x, 0.0)

    def test_forward_sign(self):
        st = InputState()
        st.begin_frame()
        snap = GamepadSnapshot(left_y=-1.0)  # stick up (SDL: -y)
        apply_gamepad(st, snap, GamepadMapping())
        self.assertGreater(st.move_y, 0.5)   # -> forward

    def test_trigger_fires(self):
        st = InputState()
        st.begin_frame()
        snap = GamepadSnapshot(right_trigger=0.9)
        apply_gamepad(st, snap, GamepadMapping())
        self.assertTrue(st.is_down(ACTION_FIRE))

    def test_button_maps_to_action(self):
        st = InputState()
        st.begin_frame()
        snap = GamepadSnapshot(buttons={"a": True})
        apply_gamepad(st, snap, GamepadMapping())
        self.assertTrue(st.is_down(ACTION_JUMP))


if __name__ == "__main__":
    unittest.main()
