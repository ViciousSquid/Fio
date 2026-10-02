"""On-screen touch-controls overlay (the *visible* virtual controls).

``player/input/touch.py`` turns finger positions into movement/look/buttons;
this module *draws* those controls so the player can see and aim for them on a
touchscreen. It renders in 2D screen space over the 3D scene using the same
translated ``simple`` GLES program the renderer already compiles, so it needs no
extra shaders.

What it draws, from the live :class:`~player.input.touch.TouchControls` layout:

* the **movement stick** — a translucent base ring (at the floating origin while
  a finger is down, or at a resting "home" spot when idle) plus a brighter knob
  offset by the current movement axis;
* each **action button** — a translucent disc that brightens while held.

Because it reads the control layout and the live :class:`InputState`, it always
matches what the input code actually does — no duplicated coordinates.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import glmath
from ..input.state import (
    InputState,
    ACTION_JUMP,
    ACTION_FIRE,
    ACTION_USE,
    ACTION_PAUSE,
)


# Per-action base colours (RGB, 0..1).
_ACTION_COLORS: Dict[str, Tuple[float, float, float]] = {
    ACTION_FIRE: (0.90, 0.30, 0.28),
    ACTION_JUMP: (0.40, 0.80, 0.45),
    ACTION_USE: (0.38, 0.62, 0.95),
    ACTION_PAUSE: (0.72, 0.72, 0.76),
}
_DEFAULT_COLOR = (0.80, 0.80, 0.85)

_CIRCLE_SEGMENTS = 40


def _gl():
    import OpenGL.GL as gl
    return gl


class OverlayRenderer:
    """Draws the touch controls with the ``simple`` program."""

    def __init__(self):
        self._program: Optional[int] = None
        self._disc_vao = None
        self._disc_vbo = None
        self._vertex_count = 0

    # ------------------------------------------------------------------
    def on_gl_ready(self, programs: Dict[str, int]) -> None:
        """Grab the simple program and (re)build the unit-disc geometry."""
        self._program = programs.get("simple")
        if self._program is None:
            print("[Overlay] 'simple' program missing; overlay disabled")
            return
        self._build_disc()

    def _build_disc(self) -> None:
        import ctypes
        import math

        import numpy as np

        gl = _gl()
        verts = [0.0, 0.0, 0.0]  # fan centre
        for i in range(_CIRCLE_SEGMENTS + 1):
            a = 2.0 * math.pi * i / _CIRCLE_SEGMENTS
            verts += [math.cos(a), math.sin(a), 0.0]
        data = np.array(verts, dtype=np.float32)
        self._vertex_count = len(verts) // 3

        self._disc_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self._disc_vao)
        self._disc_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._disc_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    # ------------------------------------------------------------------
    def render(self, controls, inp: InputState, w: int, h: int) -> None:
        if self._program is None or self._disc_vao is None:
            return
        gl = _gl()

        # 2D overlay state: no depth, alpha blending on.
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        gl.glUseProgram(self._program)
        # Screen-space projection: (0,0) top-left, (w,h) bottom-right.
        proj = glmath.ortho(0.0, float(w), float(h), 0.0, -1.0, 1.0)
        view = glmath.identity()
        self._set_mat4("projection", proj)
        self._set_mat4("view", view)

        s = min(w, h)

        # --- movement stick ------------------------------------------------
        stick = controls.move_stick
        base_r = stick.radius * s
        if stick.origin is not None:
            bx, by = stick.origin
        else:
            bx, by = 0.16 * w, 0.78 * h            # resting "home" hint
        self._disc(bx, by, base_r, (1.0, 1.0, 1.0), 0.14)
        self._disc(bx, by, base_r * 0.30,          # rim dot at centre
                   (1.0, 1.0, 1.0), 0.10)

        # Knob follows the actual movement axis (any source that moved it).
        knob_x = bx + inp.move_x * base_r
        knob_y = by - inp.move_y * base_r           # screen y is down
        active = stick.origin is not None or inp.move_x or inp.move_y
        self._disc(knob_x, knob_y, base_r * 0.42,
                   (0.95, 0.95, 1.0), 0.55 if active else 0.30)

        # --- action buttons ------------------------------------------------
        for btn in controls.buttons:
            cx, cy = btn.cx * w, btn.cy * h
            r = btn.radius * s
            color = _ACTION_COLORS.get(btn.action, _DEFAULT_COLOR)
            pressed = inp.is_down(btn.action)
            self._disc(cx, cy, r, color, 0.62 if pressed else 0.26)
            self._disc(cx, cy, r * 0.9, color, 0.30 if pressed else 0.12)

        gl.glBindVertexArray(0)
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_DEPTH_TEST)

    # ------------------------------------------------------------------
    def _disc(self, cx, cy, radius, color, alpha) -> None:
        gl = _gl()
        model = glmath.mul(glmath.translate(cx, cy, 0.0),
                           glmath.scale(radius, radius, 1.0))
        self._set_mat4("model", model)
        loc_c = gl.glGetUniformLocation(self._program, "color")
        if loc_c != -1:
            gl.glUniform3f(loc_c, float(color[0]), float(color[1]), float(color[2]))
        loc_a = gl.glGetUniformLocation(self._program, "alpha")
        if loc_a != -1:
            gl.glUniform1f(loc_a, float(alpha))
        gl.glBindVertexArray(self._disc_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, self._vertex_count)

    def _set_mat4(self, name: str, mat) -> None:
        gl = _gl()
        loc = gl.glGetUniformLocation(self._program, name)
        if loc != -1:
            gl.glUniformMatrix4fv(loc, 1, gl.GL_TRUE, mat)
