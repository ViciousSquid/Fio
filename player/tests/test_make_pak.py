"""Tests for the headless .fiopak builder (stdlib only)."""

import os
import tempfile
import unittest

from player.fiopak import FioPackage
from player.tools import make_pak

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMakePak(unittest.TestCase):
    def _map(self, name):
        path = os.path.join(REPO_ROOT, "maps", name)
        if not os.path.isfile(path):
            self.skipTest(f"repo map {name} not available")
        return path

    def test_build_and_load(self):
        map_path = self._map("Simple_Map_Test.json")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "game.fiopak")
            written, _warnings = make_pak.build_pak(
                map_path, out, REPO_ROOT, "Test Title"
            )
            self.assertTrue(os.path.isfile(out))
            with FioPackage.open(out) as pak:
                self.assertEqual(pak.title, "Test Title")
                self.assertEqual(
                    os.path.basename(pak.start_map_path()), "Simple_Map_Test.json"
                )
                level = pak.load_start_map()
                self.assertIn("brushes", level)
                # Any packaged textures are resolvable by bare filename.
                for name in pak.namelist():
                    if name.startswith("assets/"):
                        base = os.path.basename(name)
                        self.assertIsNotNone(pak.read_asset(base))

    def test_scan_asset_names(self):
        found = set()
        make_pak._scan_asset_names(
            {"textures": {"n": "wall.png"}, "model_path": "crate.obj",
             "nested": [{"sound": "boom.wav"}], "ignore": "notasset"},
            found,
        )
        self.assertEqual(found, {"wall.png", "crate.obj", "boom.wav"})


if __name__ == "__main__":
    unittest.main()
