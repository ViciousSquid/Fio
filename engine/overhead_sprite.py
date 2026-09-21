"""
Native overhead (top-down) player sprite: animation controller + ground renderer.

Used by the engine's Overhead camera mode (the editor's "Camera" dropdown) to
draw the player as a sprite lying flat on the ground that rotates to face the
player's heading, animating between an idle pose and a two-frame walk cycle.

Two pieces, split so the decision-making is testable without a GPU:

* :class:`SpriteController` — pure logic. Fed the player's ground position,
  facing angle and a clock each frame, it decides which frame to show (an *idle*
  pose when standing still; two *walk* poses alternating at ``walk_fps`` while
  moving) and carries the facing through. No GL/glm/Pillow imports.

* :class:`OverheadSpriteRenderer` — draws the chosen frame as a textured quad on
  the ground, rotated about the vertical axis to face the heading (a billboard
  can't turn within the ground plane, so this is a real rotated quad). All
  OpenGL/glm/Pillow imports are lazy and guarded; the renderer self-disables on
  any missing asset or GL error, so a missing sprite never breaks a frame.

Frames live under ``assets/sprites/topdown/`` — ``player1.png`` (idle),
``player2.png`` / ``player3.png`` (walk).
"""

from __future__ import annotations

import math
import os
from typing import Optional


# ---------------------------------------------------------------------------
# Animation state machine (pure, testable)
# ---------------------------------------------------------------------------

class SpriteController:
    """Chooses the current animation frame and tracks facing.

    Frame keys, unarmed: ``"idle"``, ``"walk_a"``, ``"walk_b"``; the
    weapon-held ("_g") variants: ``"idle_g"``, ``"walk_a_g"``, ``"walk_b_g"``;
    and ``"shoot"`` for firing. ``update`` takes the player's ground position
    ``(x, y, z)``, facing (radians, engine convention: forward =
    ``(sin a, ·, cos a)``), a monotonic ``now``, and the current
    ``armed`` / ``shooting`` state.
    """

    IDLE = "idle"
    WALK_A = "walk_a"
    WALK_B = "walk_b"
    IDLE_G = "idle_g"
    WALK_A_G = "walk_a_g"
    WALK_B_G = "walk_b_g"
    SHOOT = "shoot"

    def __init__(self, walk_fps: float = 6.0, move_epsilon: float = 0.75,
                 shoot_hold: float = 0.18):
        self.walk_fps = float(walk_fps)
        self.move_epsilon = float(move_epsilon)
        #: How long (seconds) the shoot pose is held after a shot, so a
        #: one-frame muzzle flash still reads as a visible firing pose.
        self.shoot_hold = float(shoot_hold)
        self.facing = 0.0
        self.moving = False
        self.armed = False
        self._last_pos = None
        self._anim_t = 0.0
        self._now = 0.0
        self._shoot_until = -1.0

    def update(self, pos, facing: float, now: float,
               armed: bool = False, shooting: bool = False) -> None:
        self.facing = float(facing)
        self._now = float(now)
        self.armed = bool(armed)
        p = (float(pos[0]), float(pos[1]), float(pos[2]))
        if self._last_pos is not None:
            dx = p[0] - self._last_pos[0]
            dz = p[2] - self._last_pos[2]
            self.moving = (dx * dx + dz * dz) > (self.move_epsilon * self.move_epsilon)
        else:
            self.moving = False
        if self.moving:
            self._anim_t = float(now)
        if shooting:
            self._shoot_until = float(now) + self.shoot_hold
        self._last_pos = p

    def frame(self) -> str:
        # Firing (only meaningful while armed) latches the shoot pose briefly.
        if self.armed and self._now < self._shoot_until:
            return self.SHOOT
        if not self.moving:
            base = self.IDLE
        else:
            base = self.WALK_A if int(self._anim_t * self.walk_fps) % 2 == 0 else self.WALK_B
        return (base + "_g") if self.armed else base


# ---------------------------------------------------------------------------
# Ground-plane sprite renderer (OpenGL; lazy + guarded)
# ---------------------------------------------------------------------------

_SPRITE_VERT = """#version 330 core
layout (location = 0) in vec2 aPos;   // unit quad corners in [-0.5, 0.5]
out vec2 TexCoords;
uniform mat4 mvp;
void main() {
    TexCoords = vec2(aPos.x + 0.5, 0.5 - aPos.y);   // flip V so image top = +forward
    gl_Position = mvp * vec4(aPos.x, 0.0, aPos.y, 1.0);
}"""

_SPRITE_FRAG = """#version 330 core
in vec2 TexCoords;
out vec4 FragColor;
uniform sampler2D tex;
void main() {
    vec4 c = texture(tex, TexCoords);
    if (c.a < 0.05) discard;
    FragColor = c;
}"""


def _assets_root() -> str:
    """``<repo>/assets`` resolved from this module's location."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


class OverheadSpriteRenderer:
    """Draws the player sprite as a ground quad that faces the player's heading.

    GL resources are created lazily on first :meth:`draw` (must run on the render
    thread, in the live context) and cached. Any failure disables the renderer
    for the session rather than raising into the frame loop.
    """

    def __init__(self, frame_files: Optional[dict] = None, size: float = 128.0,
                 y_offset: float = 2.0, facing_offset_deg: float = 0.0):
        # Defaults match the supplied art. Unarmed: player1 = idle, player2/3 =
        # walk. Weapon-held ("_g"): player1_g/2_g/3_g. Firing: player_shoot_g.
        _d = os.path.join(_assets_root(), "sprites", "topdown")
        self.frame_files = frame_files or {
            SpriteController.IDLE: os.path.join(_d, "player1.png"),
            SpriteController.WALK_A: os.path.join(_d, "player2.png"),
            SpriteController.WALK_B: os.path.join(_d, "player3.png"),
            SpriteController.IDLE_G: os.path.join(_d, "player1_g.png"),
            SpriteController.WALK_A_G: os.path.join(_d, "player2_g.png"),
            SpriteController.WALK_B_G: os.path.join(_d, "player3_g.png"),
            SpriteController.SHOOT: os.path.join(_d, "player_shoot_g.png"),
        }
        self.size = float(size)
        self.y_offset = float(y_offset)
        self.facing_offset_deg = float(facing_offset_deg)

        self._ok = None          # None=untried, True=ready, False=disabled
        self._prog = None
        self._vao = None
        self._vbo = None
        self._mvp_loc = -1
        self._tex_loc = -1
        self._textures: dict = {}
        # Cached module handles (bound in _init_gl) so the per-frame draw path
        # does not re-run `import` on every frame.
        self._gl = None
        self._glm = None

    # -- angle convention (pure, testable) ----------------------------------
    def facing_theta(self, facing: float) -> float:
        """Ground-plane rotation (radians) for a player *facing* angle.

        The sprite faces the player's heading when this is ``facing`` directly
        (+ a user trim). Determined empirically against the running game.
        """
        return float(facing) + math.radians(self.facing_offset_deg)

    # Graceful fallback: if a weapon-held or shoot frame is missing, fall back to
    # the closest available frame so the animation degrades instead of vanishing.
    _FALLBACKS = {
        SpriteController.SHOOT: (SpriteController.SHOOT, SpriteController.IDLE_G, SpriteController.IDLE),
        SpriteController.IDLE_G: (SpriteController.IDLE_G, SpriteController.IDLE),
        SpriteController.WALK_A_G: (SpriteController.WALK_A_G, SpriteController.WALK_A, SpriteController.IDLE_G, SpriteController.IDLE),
        SpriteController.WALK_B_G: (SpriteController.WALK_B_G, SpriteController.WALK_B, SpriteController.IDLE_G, SpriteController.IDLE),
        SpriteController.WALK_A: (SpriteController.WALK_A, SpriteController.IDLE),
        SpriteController.WALK_B: (SpriteController.WALK_B, SpriteController.IDLE),
        SpriteController.IDLE: (SpriteController.IDLE,),
    }

    def _texture_for(self, frame_key):
        for key in self._FALLBACKS.get(frame_key, (frame_key, SpriteController.IDLE)):
            tex = self._textures.get(key)
            if tex:
                return tex
        return 0

    # -- setup --------------------------------------------------------------
    def _init_gl(self) -> bool:
        try:
            import numpy as np
            import OpenGL.GL as gl
            import glm
        except Exception:
            return False
        # Cache for the per-frame draw path (see draw()).
        self._gl = gl
        self._glm = glm
        try:
            def _compile(src, kind):
                s = gl.glCreateShader(kind)
                gl.glShaderSource(s, src)
                gl.glCompileShader(s)
                if not gl.glGetShaderiv(s, gl.GL_COMPILE_STATUS):
                    raise RuntimeError(gl.glGetShaderInfoLog(s))
                return s
            vs = _compile(_SPRITE_VERT, gl.GL_VERTEX_SHADER)
            fs = _compile(_SPRITE_FRAG, gl.GL_FRAGMENT_SHADER)
            prog = gl.glCreateProgram()
            gl.glAttachShader(prog, vs); gl.glAttachShader(prog, fs)
            gl.glLinkProgram(prog)
            if not gl.glGetProgramiv(prog, gl.GL_LINK_STATUS):
                raise RuntimeError(gl.glGetProgramInfoLog(prog))
            gl.glDeleteShader(vs); gl.glDeleteShader(fs)
            self._prog = prog
            self._mvp_loc = gl.glGetUniformLocation(prog, "mvp")
            self._tex_loc = gl.glGetUniformLocation(prog, "tex")

            quad = np.array([-0.5, -0.5, 0.5, -0.5, 0.5, 0.5,
                             -0.5, -0.5, 0.5, 0.5, -0.5, 0.5], dtype=np.float32)
            self._vao = gl.glGenVertexArrays(1)
            self._vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, quad.nbytes, quad, gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)

            for key, path in self.frame_files.items():
                self._textures[key] = self._load_texture(path)
            return any(self._textures.values())
        except Exception:
            return False

    def _load_texture(self, path) -> int:
        try:
            import numpy as np
            import OpenGL.GL as gl
            from PIL import Image
        except Exception:
            return 0
        try:
            img = Image.open(path).convert("RGBA")
            data = np.array(img, dtype=np.uint8)
            tex = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height,
                            0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            return int(tex)
        except Exception:
            return 0

    # -- per-frame draw -----------------------------------------------------
    def draw(self, projection, view, pos, facing: float, frame_key: str) -> None:
        """Draw *frame_key* at ground *pos*, turned to *facing* (radians)."""
        if self._ok is False:
            return
        if self._ok is None:
            self._ok = self._init_gl()
            if not self._ok:
                return
        tex = self._texture_for(frame_key)
        if not tex:
            return
        try:
            gl = self._gl
            glm = self._glm

            theta = self.facing_theta(facing)
            m = glm.translate(glm.mat4(1.0),
                              glm.vec3(float(pos[0]), float(pos[1]) + self.y_offset, float(pos[2])))
            m = glm.rotate(m, theta, glm.vec3(0.0, 1.0, 0.0))
            m = glm.scale(m, glm.vec3(self.size, 1.0, self.size))
            mvp = projection * view * m

            gl.glUseProgram(self._prog)
            gl.glUniformMatrix4fv(self._mvp_loc, 1, gl.GL_FALSE, glm.value_ptr(mvp))
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
            gl.glUniform1i(self._tex_loc, 0)

            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glDisable(gl.GL_CULL_FACE)
            gl.glBindVertexArray(self._vao)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
            gl.glBindVertexArray(0)
            gl.glUseProgram(0)
        except Exception:
            self._ok = False
