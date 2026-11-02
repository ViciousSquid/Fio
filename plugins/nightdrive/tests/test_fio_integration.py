"""Optional full-Fio lifecycle smoke test.

In the tiny dedicated CI job these tests skip if PyQt5/PyGLM are unavailable;
on a developer checkout they exercise *real* map deserialisation, Props,
the logic-thread player and the camera lifecycle.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest

GAME = Path(__file__).resolve().parents[1]
FULL_FIO = all(importlib.util.find_spec(m) is not None
               for m in ("PyQt5", "glm", "numpy", "pygame"))


@unittest.skipUnless(FULL_FIO, "requires full Fio 3.0 runtime dependencies")
class FioIntegrationTests(unittest.TestCase):
    def test_real_level_load_and_play_session(self):
        from editor.editor_state import EditorState
        from engine.player import Player
        from engine.logic_thread import LogicThread
        from engine.threaded_game_state import ThreadedGameState
        from plugins.nightdrive.plugin import NightDriveSession, PLUGIN

        level = json.loads((GAME / "NightDrive_City.json").read_text())
        self.assertTrue(PLUGIN.map_uses_plugin(level))
        state = EditorState()
        state.load_from_data(level, save_undo=False)
        props = [t for t in state.things
                 if t.properties.get("drive_role") == "player_vehicle"]
        self.assertEqual(len(props), 1)
        logic = LogicThread(ThreadedGameState(), state)
        x, _, z = props[0].pos
        logic.player_runtime.player = Player(float(x), float(z))
        initial_camera = logic.camera.camera_mode
        saved_prop_pos = list(props[0].pos)
        session = NightDriveSession(logic)
        try:
            session.start()
            self.assertTrue(logic.camera.is_overhead())
            self.assertIsNotNone(session.vehicle)
            self.assertEqual(len(session.traffic), 12)
            self.assertEqual(len(session.checkpoints), 5)
            self.assertEqual(len(session.obstacles), 36)

            class Input:
                delta = 1 / 60
                def key_down(self, name):
                    return name == "w"
                def set_prompt(self, text, priority=0):
                    self.text = text
                def toast(self, text, seconds=2):
                    self.toast_text = text

            ctx = Input()
            for _ in range(30):
                session.tick(ctx)
            self.assertNotEqual(list(props[0].pos), saved_prop_pos)
            self.assertIn("NIGHT DRIVE", ctx.text)
            self.assertAlmostEqual(
                logic.player_runtime.player.pos.z, session.vehicle.z)
        finally:
            session.stop()
            logic.stop()
        self.assertEqual(list(props[0].pos), saved_prop_pos)
        self.assertEqual(logic.camera.camera_mode, initial_camera)


if __name__ == "__main__":
    unittest.main()
