"""Universal package player: archive, shared engine simulation and platform host."""
from __future__ import annotations

import math
import os
import sys

from player.runtime.fiopak import FioPackage, PackageError
from .platform.base import HostConfig, FrameCallbacks
from .input.state import ACTION_PAUSE


def is_android():
    return bool(os.environ.get('ANDROID_ARGUMENT')) or 'android' in sys.platform


class FioPlayerApp:
    def __init__(self, package=None, config=None, *, validation=False):
        self.package = package
        self.config = config or HostConfig(title=f'Fio Player — {package.title}' if package else 'Fio Player')
        self.renderer = self.host = self.world = self.documents = None
        self.map_data = None
        self.map_path = None
        self.cam_pos = [0, 64, 0]
        self.cam_yaw, self.cam_pitch = -90., 0.
        self._paused = package is None
        self.audio = None
        self._picker_opened = False
        self._pending_open = None
        self._validation = validation
        self._validation_elapsed = 1.

    def run(self, host=None):
        callbacks = FrameCallbacks(on_gl_ready=self._on_gl_ready, on_frame=self._on_frame,
                                   on_resize=self._on_resize, on_pause=self._on_pause,
                                   on_resume=self._on_resume, on_shutdown=self._on_shutdown)
        self.host = host or self._make_host(callbacks)
        self.host.callbacks = callbacks
        if is_android():
            from .platform.documents import AndroidDocuments
            self.documents = AndroidDocuments()
            self.host.documents = self.documents
            self.host.on_open_package = self.open_package
        try:
            self.host.run()
        finally:
            self._on_shutdown()

    def _make_host(self, callbacks):
        if is_android():
            from .platform.android import AndroidHost
            return AndroidHost(self.config, callbacks)
        from .platform.desktop import DesktopHost
        return DesktopHost(self.config, callbacks)

    def _on_gl_ready(self, host):
        from .render.renderer import GLESRenderer
        if self.renderer is None:
            self.renderer = GLESRenderer(gles=is_android())
        self.renderer.on_gl_ready()
        self.renderer.resize(host.width, host.height)
        if self.package is not None and self.world is None:
            self._load_level(self.package.start_map_path())
        if self.package is None and self.documents is not None and not self._picker_opened:
            self._picker_opened = True
            self.documents.open_world()

    def open_package(self, path):
        package = FioPackage.open(path)
        old_package = self.package
        try:
            self._load_level(package.start_map_path(), package=package)
        except Exception:
            package.close()
            raise
        self.package = package
        if old_package:
            old_package.close()

    def _load_level(self, path, spawn='', package=None, *, recover=True):
        from .world import PlayerWorld
        package = package or self.package
        data = package.load_map(path)
        previous = (self.package, self.map_path)
        loadout = self.world.logic.session_runtime.carried_loadout() if self.world else None
        # Stop the old session before rebinding the shared installed-plugin
        # manager; teardown must not run against a new session's subscriptions.
        if self.world:
            self.world.close()
        if self.audio:
            self.audio.close()
            self.audio = None
        self.world = None
        world = None
        try:
            world = PlayerWorld(data, package, spawn)
            if self.renderer:
                self.renderer.load_scene(data, package, world=world)
        except Exception:
            if world:
                world.close()
            if self.renderer:
                self.renderer.clear_scene()
            self._paused = True
            if recover and previous[0] and previous[1]:
                try:
                    self._load_level(previous[1], package=previous[0], recover=False)
                    if loadout:
                        self.world.logic.session_runtime.restore_loadout(loadout)
                except Exception:
                    self._paused = True
            raise
        self.package = package
        self.world, self.map_data, self.map_path = world, data, path
        from .audio import PlayerAudio
        self.audio = PlayerAudio(package)
        self.cam_yaw = 90 - math.degrees(world.player.angle)
        self.cam_pitch = math.degrees(world.player.pitch)
        self.cam_pos = [world.player.pos.x, world.player.pos.y + world.player.view_height, world.player.pos.z]
        self._paused = False

    def _on_frame(self, dt, inp):
        if inp.just_pressed(ACTION_PAUSE):
            self._paused = not self._paused
        if self.host:
            self.host.set_mouse_captured(bool(self.world and not self._paused))
            from .menu import hit_test
            for point in self.host.ui_events:
                action = hit_test(point, self.host.width, self.host.height, self.world is not None) if self._paused else None
                if action == 'resume':
                    self._paused = False
                elif action == 'exit':
                    self.host.request_quit()
                elif action == 'open':
                    self._open_dialog()
            self.host.ui_events.clear()
        if self.world and not self._paused:
            self.cam_yaw += inp.look_x * 120 * dt + inp.look_delta_x
            self.cam_pitch = max(-89, min(89, self.cam_pitch + inp.look_y * 120 * dt + inp.look_delta_y))
            self.cam_pos = self.world.step(dt, inp, self.cam_yaw, self.cam_pitch)
            # Portal transit and cutscenes may change the authoritative pose.
            self.cam_yaw = 90 - math.degrees(self.world.player.angle)
            self.cam_pitch = math.degrees(self.world.player.pitch)
            self._advance_level()
        if self.audio and self.world:
            self.audio.pause(self._paused)
            self.audio.process(self.world.game_state.consume_sounds(), self.cam_pos, self.cam_yaw)
        if self.renderer:
            state = self._render_state()
            self.renderer.render(state)
            if self.config.show_touch_controls and self.host and not self._paused:
                self.renderer.render_overlay(self.host.touch, inp, self.host.width, self.host.height)
            state["control_buttons"] = tuple((b.action, b.cx, b.cy) for b in self.host.touch.buttons) if self.config.show_touch_controls else ()
            self.renderer.render_ui(state, self.host.width, self.host.height, self.world is not None)
        if self._validation and self.world:
            self._validation_elapsed += dt
            if self._validation_elapsed >= 1.:
                import json
                self._validation_elapsed = 0.
                print('[FIO_VALIDATION] ' + json.dumps({
                    'map': self.map_path, 'paused': self._paused,
                    'input_blocked': bool(self.world.logic.cutscene_runtime.state),
                    'movement_input': [inp.move_x, inp.move_y],
                    'touches': len(self.host._pointers),
                    'controllers': len(self.host._joysticks),
                    'position': [float(v) for v in self.world.player.pos],
                    'brushes': len(self.world.brushes),
                    'entities': len(self.world.state.things),
                }), flush=True)

    def _open_dialog(self):
        if self.documents:
            self.documents.open_world()
            return
        from PyQt5.QtWidgets import QApplication, QFileDialog
        application = QApplication.instance() or QApplication([])
        path, _ = QFileDialog.getOpenFileName(None, 'Open world', '', 'Fio worlds (*.fiopak)')
        if path:
            self.open_package(path)

    def _advance_level(self):
        if self.world.pending_level:
            target, spawn = self.world.pending_level
            self.world.pending_level = None
            path = self.package.resolve_map(target, self.map_path)
            loadout = self.world.logic.session_runtime.carried_loadout()
            self._load_level(path, spawn)
            self.world.logic.session_runtime.restore_loadout(loadout)

    def _render_state(self):
        state = dict(cam_pos=self.cam_pos, cam_yaw=self.cam_yaw, cam_pitch=self.cam_pitch,
                     paused=self._paused, hud=self.world.hud if self.world else 'Open a .fiopak world')
        if self.world:
            eye, direction, up, fov = self.world.camera()
            from .render import glmath
            state.update(cam_pos=eye, view=glmath.look_at(eye, eye + direction, up), fov=fov, world=self.world)
        return state

    def _on_resize(self, width, height):
        if self.renderer:
            self.renderer.resize(width, height)
        if self.world:
            self.world.logic.camera.set_frustum_aspect(width / max(1, height))

    def _on_pause(self):
        self._paused = True
        if self.audio:
            self.audio.pause(True)

    def _on_resume(self):
        self._paused = True

    def _on_shutdown(self):
        if self.audio:
            self.audio.close()
        if self.world:
            self.world.close()
            self.world = None
        if self.renderer:
            lifecycle = getattr(self.host, 'lifecycle', None)
            if lifecycle is None or lifecycle.surface_valid:
                self.renderer.close()
            self.renderer = None
        if self.documents:
            self.documents.close()
        if self.package:
            self.package.close()
