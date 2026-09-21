"""Desktop development host (pygame / SDL2, OpenGL ES context).

This is the on-desktop harness for developing the mobile player. It requests an
**OpenGL ES** context from SDL2 so the exact same translated shaders and GL-ES
call patterns run here as on the device — the whole point being that "works on
my laptop" means "works on the phone".

pygame is imported lazily so the rest of the ``player`` package (loader, shader
translation, input model) stays importable — and unit-testable — on machines
without pygame or a display.
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

from .base import PlatformHost, HostConfig, FrameCallbacks
from .lifecycle import LifecycleState
from ..input.state import (
    InputState,
    ACTION_JUMP,
    ACTION_FIRE,
    ACTION_USE,
    ACTION_CROUCH,
    ACTION_SPRINT,
    ACTION_PAUSE,
)
from ..input.touch import TouchControls
from ..input.gamepad import GamepadMapping, GamepadSnapshot, apply_gamepad


class DesktopHost(PlatformHost):
    """A pygame window running the player frame loop."""

    # Desktop keyboard convenience mapping (dev harness only).
    _KEY_ACTIONS = {
        "space": ACTION_JUMP,
        "left ctrl": ACTION_CROUCH,
        "left shift": ACTION_SPRINT,
        "e": ACTION_USE,
        "escape": ACTION_PAUSE,
    }

    def __init__(self, config: HostConfig, callbacks: FrameCallbacks):
        super().__init__(config, callbacks)
        self.lifecycle = LifecycleState()
        self.touch = TouchControls()
        self.gamepad_mapping = GamepadMapping()
        self._running = False
        self._pointers: Dict[int, Tuple[float, float]] = {}
        self._pg = None
        self._surface = None
        self._joysticks: list = []
        self._mouse_look = False

    # ------------------------------------------------------------------
    def run(self) -> None:
        import pygame  # lazy

        self._pg = pygame
        pygame.init()
        try:
            pygame.joystick.init()
        except Exception:
            pass

        self._configure_gl_attributes()
        flags = pygame.OPENGL | pygame.DOUBLEBUF
        if self.config.fullscreen:
            flags |= pygame.FULLSCREEN
        self._surface = pygame.display.set_mode(
            (self.config.width, self.config.height), flags
        )
        pygame.display.set_caption(self.config.title)
        self.width, self.height = self._surface.get_size()

        self.lifecycle.on_surface_created()
        self.callbacks.on_gl_ready(self)
        self.callbacks.on_resize(self.width, self.height)

        self._open_gamepads()
        self._running = True
        self._loop()

        self.callbacks.on_shutdown()
        pygame.quit()

    def _configure_gl_attributes(self) -> None:
        pg = self._pg
        A = pg.GL_CONTEXT_MAJOR_VERSION
        pg.display.gl_set_attribute(pg.GL_CONTEXT_MAJOR_VERSION, self.config.gles_major)
        pg.display.gl_set_attribute(pg.GL_CONTEXT_MINOR_VERSION, self.config.gles_minor)
        # Request an ES profile so desktop mirrors the device dialect.
        try:
            pg.display.gl_set_attribute(
                pg.GL_CONTEXT_PROFILE_MASK, pg.GL_CONTEXT_PROFILE_ES
            )
        except Exception:
            # Some SDL builds lack ES profiles; fall back to core so the harness
            # still runs (shaders are ES-source but GL will accept them).
            pass
        pg.display.gl_set_attribute(pg.GL_DEPTH_SIZE, self.config.depth_bits)
        pg.display.gl_set_attribute(pg.GL_STENCIL_SIZE, self.config.stencil_bits)
        pg.display.gl_set_attribute(pg.GL_DOUBLEBUFFER, 1)

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        pg = self._pg
        target_dt = 1.0 / max(self.config.target_fps, 1)
        last = time.perf_counter()
        while self._running and self.lifecycle.is_alive:
            now = time.perf_counter()
            dt = now - last
            last = now

            self.input_state.begin_frame()
            self._pump_events()
            self._read_keyboard_mouse()
            self._read_gamepads()
            self.touch.apply(self.input_state, self._pointers, self.width, self.height)

            if self.lifecycle.consume_gl_reload():
                self.callbacks.on_gl_ready(self)

            if self.lifecycle.should_simulate:
                self.callbacks.on_frame(dt, self.input_state)

            if self.lifecycle.should_render:
                self.swap_buffers()
            else:
                # Backgrounded: yield the CPU instead of spinning.
                time.sleep(0.05)

            # Soft frame cap when vsync is unavailable.
            if not self.config.vsync:
                slack = target_dt - (time.perf_counter() - now)
                if slack > 0:
                    time.sleep(slack)

    def swap_buffers(self) -> None:
        self._pg.display.flip()

    def request_quit(self) -> None:
        self._running = False
        self.lifecycle.on_stop()

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def _pump_events(self) -> None:
        pg = self._pg
        focus_lost = getattr(pg, "WINDOWFOCUSLOST", -1)
        focus_gained = getattr(pg, "WINDOWFOCUSGAINED", -1)
        for event in pg.event.get():
            et = event.type
            if et == pg.QUIT:
                self.request_quit()
            elif et == pg.VIDEORESIZE:
                self._on_resize(event.w, event.h)
            elif et == focus_lost:
                self._pause()
            elif et == focus_gained:
                self._resume()
            elif et in (getattr(pg, "FINGERDOWN", -1), getattr(pg, "FINGERMOTION", -1)):
                self._pointers[event.finger_id] = (
                    event.x * self.width,
                    event.y * self.height,
                )
            elif et == getattr(pg, "FINGERUP", -1):
                self._pointers.pop(event.finger_id, None)
            elif et == pg.MOUSEBUTTONDOWN and event.button == 1:
                self._mouse_look = True
            elif et == pg.MOUSEBUTTONUP and event.button == 1:
                self._mouse_look = False
            elif et == pg.MOUSEMOTION and self._mouse_look:
                dx, dy = event.rel
                self.input_state.add_look(dx * 0.15, -dy * 0.15)

    def _read_keyboard_mouse(self) -> None:
        pg = self._pg
        keys = pg.key.get_pressed()
        mx = my = 0.0
        if keys[pg.K_w]:
            my += 1.0
        if keys[pg.K_s]:
            my -= 1.0
        if keys[pg.K_d]:
            mx += 1.0
        if keys[pg.K_a]:
            mx -= 1.0
        if mx or my:
            self.input_state.add_move(mx, my)
        for keyname, action in self._KEY_ACTIONS.items():
            if keys[pg.key.key_code(keyname)]:
                self.input_state.set_button(action, True)
        if pg.mouse.get_pressed()[0]:
            self.input_state.set_button(ACTION_FIRE, True)

    # ------------------------------------------------------------------
    # Gamepad
    # ------------------------------------------------------------------
    def _open_gamepads(self) -> None:
        pg = self._pg
        self._joysticks = []
        for i in range(pg.joystick.get_count()):
            js = pg.joystick.Joystick(i)
            js.init()
            self._joysticks.append(js)

    def _read_gamepads(self) -> None:
        if not self._joysticks:
            return
        js = self._joysticks[0]
        try:
            snap = GamepadSnapshot(
                left_x=js.get_axis(0),
                left_y=js.get_axis(1),
                right_x=js.get_axis(2) if js.get_numaxes() > 2 else 0.0,
                right_y=js.get_axis(3) if js.get_numaxes() > 3 else 0.0,
                left_trigger=self._axis01(js, 4),
                right_trigger=self._axis01(js, 5),
                buttons=self._gamepad_buttons(js),
            )
        except Exception:
            return
        apply_gamepad(self.input_state, snap, self.gamepad_mapping)

    @staticmethod
    def _axis01(js, idx: int) -> float:
        if js.get_numaxes() <= idx:
            return 0.0
        # SDL triggers rest at -1 and press to +1; remap to 0..1.
        return (js.get_axis(idx) + 1.0) * 0.5

    @staticmethod
    def _gamepad_buttons(js) -> Dict[str, bool]:
        names = ["a", "b", "x", "y", "leftshoulder", "rightshoulder",
                 "back", "start", "leftstick", "rightstick"]
        out: Dict[str, bool] = {}
        for i, name in enumerate(names):
            if i < js.get_numbuttons():
                out[name] = bool(js.get_button(i))
        return out

    # ------------------------------------------------------------------
    # Lifecycle helpers (overridden / extended by AndroidHost)
    # ------------------------------------------------------------------
    def _on_resize(self, w: int, h: int) -> None:
        self.width, self.height = w, h
        self.callbacks.on_resize(w, h)

    def _pause(self) -> None:
        if self.lifecycle.should_simulate:
            self.lifecycle.on_pause()
            self.touch.reset()
            self.callbacks.on_pause()

    def _resume(self) -> None:
        self.lifecycle.on_resume()
        self.callbacks.on_resume()
