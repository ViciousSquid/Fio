"""Tests for pure-logic platform helpers (lifecycle, adaptive resolution)."""

import unittest

from player.platform.lifecycle import LifecycleState, LifecyclePhase
from player.platform.adaptive import AdaptiveResolution


class TestLifecycle(unittest.TestCase):
    def test_start_runs(self):
        lc = LifecycleState()
        self.assertFalse(lc.should_render)
        lc.on_surface_created()
        self.assertEqual(lc.phase, LifecyclePhase.RUNNING)
        self.assertTrue(lc.should_render)
        self.assertTrue(lc.should_simulate)

    def test_pause_halts_sim_and_render(self):
        lc = LifecycleState()
        lc.on_surface_created()
        lc.on_pause()
        self.assertFalse(lc.should_simulate)
        self.assertFalse(lc.should_render)

    def test_resume_with_surface(self):
        lc = LifecycleState()
        lc.on_surface_created()
        lc.on_pause()
        lc.on_resume()
        self.assertTrue(lc.should_render)

    def test_surface_loss_forces_gl_reload_on_recreate(self):
        lc = LifecycleState()
        lc.on_surface_created()
        lc.on_pause()
        lc.on_surface_destroyed()
        self.assertEqual(lc.phase, LifecyclePhase.SURFACE_LOST)
        self.assertFalse(lc.should_render)
        # Coming back: surface recreated -> needs GL reload flagged once.
        lc.on_surface_created()
        self.assertTrue(lc.consume_gl_reload())
        self.assertFalse(lc.consume_gl_reload())  # consumed
        self.assertTrue(lc.should_render)

    def test_stop_ends_life(self):
        lc = LifecycleState()
        lc.on_surface_created()
        lc.on_stop()
        self.assertFalse(lc.is_alive)


class TestAdaptiveResolution(unittest.TestCase):
    def test_scale_drops_when_slow(self):
        ar = AdaptiveResolution(target_fps=60.0, min_scale=0.5, step=0.05)
        start = ar.scale
        # Feed consistently slow frames (30 fps => 33ms).
        for _ in range(50):
            ar.submit_frame(33.0)
        self.assertLess(ar.scale, start)
        self.assertGreaterEqual(ar.scale, 0.5)

    def test_scale_rises_when_fast(self):
        ar = AdaptiveResolution(target_fps=60.0, min_scale=0.5, max_scale=1.0)
        ar.scale = 0.6
        for _ in range(200):
            ar.submit_frame(5.0)  # very fast frames
        self.assertGreater(ar.scale, 0.6)
        self.assertLessEqual(ar.scale, 1.0)

    def test_render_size_even_and_scaled(self):
        ar = AdaptiveResolution()
        ar.scale = 0.5
        w, h = ar.render_size(1080, 1920)
        self.assertEqual((w, h), (540, 960))
        self.assertEqual(w % 2, 0)
        self.assertEqual(h % 2, 0)

    def test_zero_frame_ignored(self):
        ar = AdaptiveResolution()
        before = ar.scale
        self.assertEqual(ar.submit_frame(0.0), before)


if __name__ == "__main__":
    unittest.main()
