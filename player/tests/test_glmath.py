"""Tests for the numpy matrix helpers (skipped when numpy is unavailable)."""

import unittest

try:
    import numpy as np
    from player.render import glmath
    HAVE_NUMPY = True
except Exception:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class TestGlMath(unittest.TestCase):
    def test_identity(self):
        m = glmath.identity()
        self.assertEqual(m.shape, (4, 4))
        self.assertTrue(np.allclose(m, np.eye(4)))

    def test_translate_maps_origin(self):
        m = glmath.translate(3, 4, 5)
        p = m @ np.array([0, 0, 0, 1], dtype=np.float32)
        self.assertTrue(np.allclose(p[:3], [3, 4, 5]))

    def test_scale(self):
        m = glmath.scale(2, 3, 4)
        p = m @ np.array([1, 1, 1, 1], dtype=np.float32)
        self.assertTrue(np.allclose(p[:3], [2, 3, 4]))

    def test_ortho_corners(self):
        # ortho(0,w,h,0): screen (0,0) -> NDC (-1, 1); (w,h) -> (1, -1).
        w, h = 800.0, 600.0
        m = glmath.ortho(0, w, h, 0)
        tl = m @ np.array([0, 0, 0, 1], dtype=np.float32)
        br = m @ np.array([w, h, 0, 1], dtype=np.float32)
        self.assertTrue(np.allclose(tl[:2], [-1, 1]))
        self.assertTrue(np.allclose(br[:2], [1, -1]))

    def test_mul_matches_chained_matmul(self):
        a = glmath.translate(1, 2, 0)
        b = glmath.scale(2, 2, 1)
        self.assertTrue(np.allclose(glmath.mul(a, b), a @ b))

    def test_dtype_is_float32(self):
        self.assertEqual(glmath.mul(glmath.identity(), glmath.translate(1, 0)).dtype,
                         np.float32)


if __name__ == "__main__":
    unittest.main()
