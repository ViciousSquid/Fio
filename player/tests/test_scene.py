"""Tests for the map->geometry builder and camera math (skip without numpy)."""

import math
import unittest

try:
    import numpy as np
    from player.render import scene, glmath
    HAVE_NUMPY = True
except Exception:
    HAVE_NUMPY = False


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class TestSceneMesh(unittest.TestCase):
    def _map(self, brushes=None, things=None):
        return {"version": 3, "brushes": brushes or [], "things": things or []}

    def test_one_brush_makes_36_verts(self):
        m = self._map([{"pos": [0, 0, 0], "size": [10, 10, 10], "id": 1}])
        verts, count = scene.build_brush_mesh(m)
        self.assertEqual(count, 36)
        self.assertEqual(verts.reshape(-1, 6).shape, (36, 6))

    def test_vertices_within_bounds(self):
        m = self._map([{"pos": [100, 50, -20], "size": [20, 40, 60]}])
        verts, count = scene.build_brush_mesh(m)
        pts = verts.reshape(-1, 6)[:, :3]
        self.assertTrue(np.all(pts[:, 0] >= 90 - 1e-4) and np.all(pts[:, 0] <= 110 + 1e-4))
        self.assertTrue(np.all(pts[:, 1] >= 30 - 1e-4) and np.all(pts[:, 1] <= 70 + 1e-4))
        self.assertTrue(np.all(pts[:, 2] >= -50 - 1e-4) and np.all(pts[:, 2] <= 10 + 1e-4))

    def test_hidden_and_trigger_skipped(self):
        m = self._map([
            {"pos": [0, 0, 0], "size": [1, 1, 1], "hidden": True},
            {"pos": [0, 0, 0], "size": [1, 1, 1], "is_trigger": True},
            {"pos": [0, 0, 0], "size": [1, 1, 1]},
        ])
        _, count = scene.build_brush_mesh(m)
        self.assertEqual(count, 36)  # only the plain brush

    def test_normals_are_unit(self):
        m = self._map([{"pos": [0, 0, 0], "size": [2, 2, 2]}])
        verts, _ = scene.build_brush_mesh(m)
        normals = verts.reshape(-1, 6)[:, 3:]
        lengths = np.linalg.norm(normals, axis=1)
        self.assertTrue(np.allclose(lengths, 1.0))

    def test_extract_lights(self):
        m = self._map(things=[
            {"type": "light", "pos": [1, 2, 3],
             "properties": {"colour": [255, 128, 0], "intensity": 2.0, "radius": 300}},
            {"type": "light", "pos": [0, 0, 0], "properties": {"state": "off"}},
            {"type": "PlayerStart", "pos": [0, 0, 0]},
        ])
        lights = scene.extract_lights(m)
        self.assertEqual(len(lights), 1)  # the "off" light and non-light excluded
        self.assertEqual(lights[0]["pos"], (1.0, 2.0, 3.0))
        self.assertAlmostEqual(lights[0]["color"][0], 1.0)
        self.assertAlmostEqual(lights[0]["color"][1], 128 / 255)
        self.assertEqual(lights[0]["intensity"], 2.0)

    def test_light_limit(self):
        things = [{"type": "light", "pos": [i, 0, 0], "properties": {}} for i in range(20)]
        self.assertEqual(len(scene.extract_lights(self._map(things=things), limit=8)), 8)

    def test_textured_batches_group_by_texture(self):
        m = self._map([
            {"pos": [0, 0, 0], "size": [10, 10, 10],
             "textures": {k: "wall.png" for k in
                          ("north", "south", "east", "west", "top", "down")}},
        ])
        batches = scene.build_textured_batches(m)
        self.assertIn("wall.png", batches)
        verts = batches["wall.png"]
        self.assertEqual(verts.size % 8, 0)          # 8 floats per vertex
        self.assertEqual(verts.size // 8, 36)        # all 6 faces, one texture

    def test_textured_batches_split_and_uv_tiling(self):
        # A 256-wide face at texel=128 should tile the U axis twice (0..2).
        m = self._map([
            {"pos": [0, 0, 0], "size": [256, 10, 10],
             "textures": {"top": "a.png", "down": "b.png"}},
        ])
        batches = scene.build_textured_batches(m, texel=128.0)
        self.assertEqual(set(batches) - {""}, {"a.png", "b.png"})
        uv = batches["a.png"].reshape(-1, 8)[:, 6:]  # top face spans X(256)/Z(10)
        self.assertAlmostEqual(uv[:, 0].max(), 2.0, places=4)   # U tiles twice

    def test_untextured_faces_use_empty_key(self):
        m = self._map([{"pos": [0, 0, 0], "size": [10, 10, 10]}])  # no textures
        batches = scene.build_textured_batches(m)
        self.assertEqual(set(batches), {""})

    def test_bounds(self):
        m = self._map([{"pos": [0, 0, 0], "size": [10, 10, 10]},
                       {"pos": [100, 0, 0], "size": [10, 10, 10]}])
        lo, hi = scene.scene_bounds(m)
        self.assertEqual(lo[0], -5.0)
        self.assertEqual(hi[0], 105.0)


@unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
class TestCameraMath(unittest.TestCase):
    def test_perspective_shape(self):
        p = glmath.perspective(math.radians(70), 16 / 9, 1.0, 1000.0)
        self.assertEqual(p.shape, (4, 4))
        self.assertEqual(p[3, 2], -1.0)      # perspective divide row
        self.assertEqual(p[3, 3], 0.0)

    def test_look_at_eye_maps_to_origin(self):
        # A point at the eye position transforms to the view-space origin.
        eye = [10.0, 5.0, 3.0]
        view = glmath.look_at(eye, [10.0, 5.0, -100.0])
        p = view @ np.array([eye[0], eye[1], eye[2], 1.0], dtype=np.float32)
        self.assertTrue(np.allclose(p[:3], [0, 0, 0], atol=1e-4))

    def test_front_from_angles(self):
        # yaw -90, pitch 0 -> looking down -Z (engine default).
        f = glmath.front_from_angles(-90.0, 0.0)
        self.assertAlmostEqual(f[0], 0.0, places=5)
        self.assertAlmostEqual(f[1], 0.0, places=5)
        self.assertAlmostEqual(f[2], -1.0, places=5)


if __name__ == "__main__":
    unittest.main()
