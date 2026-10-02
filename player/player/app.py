"""FioPlayerApp — composes loader + host + renderer into a running player.

Flow:

    package = FioPackage.open("game.fiopak")
    app = FioPlayerApp(package)
    app.run()                      # picks a host, blocks until quit

The app owns the game-facing state (camera, and later the engine player /
world simulation) and exposes the host callbacks. The input -> camera mapping
here is a lightweight free-look controller sufficient for milestones 1-2; the
seam :meth:`_advance_simulation` is where the engine's ``player.py`` /
``logic_thread.py`` world simulation is driven once ported.
"""

from __future__ import annotations

import math
from typing import Optional

from .fiopak import FioPackage
from .platform.base import HostConfig, FrameCallbacks
from .input.state import InputState, ACTION_PAUSE


class FioPlayerApp:
    def __init__(self, package: Optional[FioPackage] = None,
                 config: Optional[HostConfig] = None):
        self.package = package
        title = f"Fio Player — {package.title}" if package is not None else "Fio Player"
        self.config = config or HostConfig(title=title)
        self.renderer = None            # created on GL ready (needs a context)
        self.host = None
        self.map_data = None

        # Free-look camera state (world units). Yaw/pitch in degrees.
        self.cam_pos = [0.0, 64.0, 0.0]
        self.cam_yaw = -90.0
        self.cam_pitch = 0.0
        self.move_speed = 300.0         # units / second
        self.turn_speed = 120.0         # degrees / second
        self._paused = False

    # ------------------------------------------------------------------
    def run(self, host=None) -> None:
        """Create a host (auto-detected if not supplied) and run the loop."""
        callbacks = FrameCallbacks(
            on_gl_ready=self._on_gl_ready,
            on_frame=self._on_frame,
            on_resize=self._on_resize,
            on_pause=self._on_pause,
            on_resume=self._on_resume,
            on_shutdown=self._on_shutdown,
        )
        if host is None:
            host = self._make_host(callbacks)
        self.host = host
        host.run()

    def _make_host(self, callbacks: FrameCallbacks):
        if is_android():
            from .platform.android import AndroidHost

            return AndroidHost(self.config, callbacks)
        from .platform.desktop import DesktopHost

        return DesktopHost(self.config, callbacks)

    # ------------------------------------------------------------------
    # Host callbacks
    # ------------------------------------------------------------------
    def _on_gl_ready(self, host) -> None:
        from .render.renderer import GLESRenderer

        if self.renderer is None:
            self.renderer = GLESRenderer()
        self.renderer.on_gl_ready()
        self.renderer.resize(host.width, host.height)

        # Load the start map the first time we have a context. With no package
        # (a bare test build), stay in "empty" mode: the renderer shows the
        # reference triangle and the on-screen controls so the APK still runs.
        if self.package is not None and self.map_data is None:
            self.map_data = self.package.load_start_map()
            self._place_camera_at_spawn()
            self.renderer.load_scene(self.map_data, self.package)

    def _on_resize(self, w: int, h: int) -> None:
        if self.renderer is not None:
            self.renderer.resize(w, h)

    def _on_frame(self, dt: float, inp: InputState) -> None:
        if inp.just_pressed(ACTION_PAUSE):
            self._paused = not self._paused
        if not self._paused:
            self._apply_input_to_camera(dt, inp)
            self._advance_simulation(dt, inp)
        if self.renderer is not None:
            self.renderer.render(self._render_state())
            # Draw the on-screen touch controls last, over the scene.
            if self.config.show_touch_controls and self.host is not None:
                touch = getattr(self.host, "touch", None)
                if touch is not None:
                    self.renderer.render_overlay(
                        touch, inp, self.host.width, self.host.height
                    )

    def _on_pause(self) -> None:
        self._paused = True
        # TODO(port): pause engine audio (engine/audio_manager.py) here.

    def _on_resume(self) -> None:
        # Stay paused until the player taps; avoids a jarring resume.
        self._paused = True

    def _on_shutdown(self) -> None:
        if self.package is not None:
            try:
                self.package.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Camera / simulation
    # ------------------------------------------------------------------
    def _apply_input_to_camera(self, dt: float, inp: InputState) -> None:
        self.cam_yaw += inp.look_x * self.turn_speed * dt
        self.cam_pitch += inp.look_y * self.turn_speed * dt
        self.cam_pitch = max(-89.0, min(89.0, self.cam_pitch))

        yaw = math.radians(self.cam_yaw)
        fwd = (math.cos(yaw), math.sin(yaw))          # ground-plane forward (x,z)
        right = (-fwd[1], fwd[0])
        speed = self.move_speed * dt
        self.cam_pos[0] += (fwd[0] * inp.move_y + right[0] * inp.move_x) * speed
        self.cam_pos[2] += (fwd[1] * inp.move_y + right[1] * inp.move_x) * speed

    def _advance_simulation(self, dt: float, inp: InputState) -> None:
        # TODO(port): step engine world simulation (physics, AI, logic) here,
        # feeding `inp` into engine/player.py instead of the free-look camera.
        pass

    def _place_camera_at_spawn(self) -> None:
        """Position the camera at a PlayerStart-like entity if present."""
        if not self.map_data:
            return
        for thing in self.map_data.get("things", []):
            if not isinstance(thing, dict):
                continue
            ttype = str(thing.get("type", "")).lower()
            if "start" in ttype or "spawn" in ttype or ttype == "player":
                pos = thing.get("pos")
                if isinstance(pos, (list, tuple)) and len(pos) == 3:
                    self.cam_pos = [float(pos[0]), float(pos[1]) + 48.0, float(pos[2])]
                    return

    def _render_state(self):
        return {
            "cam_pos": tuple(self.cam_pos),
            "cam_yaw": self.cam_yaw,
            "cam_pitch": self.cam_pitch,
            "paused": self._paused,
        }


# ----------------------------------------------------------------------
def is_android() -> bool:
    """True when running inside a python-for-android APK."""
    import os
    import sys

    return bool(os.environ.get("ANDROID_ARGUMENT")) or "android" in sys.platform
