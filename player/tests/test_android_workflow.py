import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import subprocess
import sys

import numpy as np

from player.runtime.fiopak import FioPackage, PackageError
from player.platform.documents import copy_package
from player.render import glmath
from player.render.scene import build_textured_batches
from player.world import PlayerWorld
from player.input.state import InputState, ACTION_JUMP
from player.platform.base import HostConfig, FrameCallbacks
from player.platform.desktop import DesktopHost


class TestAndroidWorkflow(unittest.TestCase):
    def test_android_startup_keeps_activity_callbacks_unblocked(self):
        import os
        from unittest.mock import patch
        from player import main as entry
        with patch.dict(os.environ, {'ANDROID_ARGUMENT': '/android/app',
                                     'SDL_ANDROID_BLOCK_ON_PAUSE': '1',
                                     'SDL_ACCELEROMETER_AS_JOYSTICK': '1'}, clear=True):
            with patch.object(entry, '_main', return_value=0):
                self.assertEqual(entry.main([]), 0)
            self.assertEqual(os.environ['SDL_ANDROID_BLOCK_ON_PAUSE'], '0')
            self.assertEqual(os.environ['SDL_AUDIODRIVER'], 'aaudio,android')
            self.assertEqual(os.environ['PYOPENGL_PLATFORM'], 'egl')
            self.assertEqual(os.environ['SDL_ACCELEROMETER_AS_JOYSTICK'], '0')

    def test_document_result_is_applied_after_context_recreation(self):
        order = []
        host = DesktopHost(HostConfig(target_fps=1000), FrameCallbacks())
        host._pg = object()
        host._running = True
        host.lifecycle.on_surface_created()
        host.lifecycle.on_surface_destroyed()
        host._pump_events = host.lifecycle.on_surface_created
        host._read_keyboard_mouse = host._read_gamepads = lambda: None
        host._before_frame = lambda: order.append('documents')
        host.callbacks.on_gl_ready = lambda _: order.append('gl')
        def frame(dt, inp):
            order.append('frame')
            host.request_quit()
        host.callbacks.on_frame = frame
        host._loop()
        self.assertEqual(order, ['gl', 'documents', 'frame'])

    def test_android_events_do_not_open_worlds_before_foreground_gl(self):
        from types import SimpleNamespace
        from player.platform.android import AndroidHost
        host = AndroidHost(HostConfig(), FrameCallbacks())
        host._pg = SimpleNamespace(event=SimpleNamespace(get=lambda: []))
        class Documents:
            def drain(self, *args):
                raise AssertionError('World upload ran during event processing')
        host.documents = Documents()
        host._pump_events()

    def test_android_entrypoint_does_not_import_desktop_helpers(self):
        script = '''
import importlib.abc
import os
import runpy
import sys
import types
class NoDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('editor', 'PyQt5', 'load_throbber'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoDesktop())
os.environ['ANDROID_ARGUMENT'] = '/data/user/0/fio/files/app'
stub = types.ModuleType('player.main')
stub.main = lambda argv: 0
sys.modules['player.main'] = stub
runpy.run_path('main.py', run_name='__main__')
'''
        subprocess.run([sys.executable, "-c", script], check=True,
                       capture_output=True, text=True)

    def test_packaged_player_imports_without_desktop_ui(self):
        script = '''
import importlib.abc
import sys
class NoEditor(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'PyQt5':
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoEditor())
from player.app import FioPlayerApp
from player.world import PlayerWorld
from player.input.state import InputState
FioPlayerApp()
world = PlayerWorld({'brushes': [], 'things': [{'type': 'PlayerStart', 'pos': [0, 128, 0]}]})
try:
    world.step(1/30, InputState(), 0, 0)
finally:
    world.close()
'''
        subprocess.run([sys.executable, "-c", script], check=True,
                       capture_output=True, text=True)

    def test_matrix_upload_preserves_translation_without_es_transpose(self):
        matrix = glmath.translate(12, 34, 56) @ glmath.rotate_z(0.4)
        uploaded = glmath.column_major(matrix)
        # GL reads contiguous memory column-first, with transpose=false.
        reconstructed = uploaded.ravel().reshape((4, 4), order="F")
        np.testing.assert_allclose(reconstructed, matrix)
        np.testing.assert_allclose(reconstructed @ [0, 0, 0, 1], [12, 34, 56, 1])

    def test_provider_copy_rejects_oversize_without_destroying_previous_world(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "world.fiopak"
            path.write_bytes(b"previous world")
            with self.assertRaises(ValueError):
                copy_package(io.BytesIO(b"too large"), path, limit=3)
            self.assertEqual(path.read_bytes(), b"previous world")
            self.assertFalse(path.with_suffix(".partial").exists())
            copy_package(io.BytesIO(b"replacement"), path)
            self.assertEqual(path.read_bytes(), b"replacement")

    def test_clipped_brush_uses_engine_surface_not_bounding_box(self):
        from engine.brush_geometry import box_to_geometry, clip_brush, get_convex
        brush = {"pos": [0, 0, 0], "size": [100, 100, 100]}
        box_to_geometry(brush)
        clip_brush(brush, [1, 1, 0], 20)
        vertices = np.concatenate(list(build_textured_batches({"brushes": [brush]}).values())).reshape(-1, 8)
        self.assertGreater(len(vertices), 0)
        # Actual vertex positions must belong to the engine convex geometry.
        corners = get_convex(brush).verts
        for point in vertices[:, :3]:
            self.assertLess(np.linalg.norm(corners - point, axis=1).min(), 0.001)

    def test_engine_controller_lands_jumps_and_collides(self):
        data = {"brushes": [
            {"pos": [0, -16, 0], "size": [2048, 32, 2048]},
            {"pos": [0, 100, -200], "size": [1024, 200, 32]},
        ], "things": [{"type": "PlayerStart", "pos": [0, 128, 0]}]}
        world = PlayerWorld(data)
        inp = InputState()
        for _ in range(120):
            world.step(1/60, inp, -90, 0)
        self.assertTrue(world.player.on_ground)
        ground = world.player.pos.y
        inp.set_button(ACTION_JUMP, True)
        for _ in range(6):
            world.step(1/60, inp, -90, 0)
        self.assertGreater(world.player.pos.y, ground + 5)
        inp.set_button(ACTION_JUMP, False)
        inp.move_y = 1
        for _ in range(180):
            world.step(1/60, inp, -90, 0)
        self.assertGreater(world.player.pos.z, -184)
        self.assertLess(world.player.pos.z, -80)

    def test_bom_manifest_and_non_object_map(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("metadata.json", '\ufeff' + json.dumps({"map_path": "maps/start.json"}))
            zf.writestr("maps/start.json", '[]')
        with FioPackage.from_bytes(archive.getvalue()) as package:
            with self.assertRaisesRegex(PackageError, "JSON object"):
                package.load_start_map()
