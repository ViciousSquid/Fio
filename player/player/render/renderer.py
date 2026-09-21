"""Milestone-1 OpenGL ES renderer + engine bridge seam.

Two responsibilities:

1. **Proof-of-life ES pipeline** (implemented): create the ES viewport, compile
   the translated shader set, draw a reference triangle through the ``simple``
   program, and draw the touch-controls overlay on top. When you see the
   triangle and the on-screen controls on-device, the whole chain works: ES
   context, GLSL-ES translation, VBO/VAO, uniforms, blending, draw, present.

2. **Engine bridge** (seam): :meth:`load_scene` and :meth:`render_scene` are the
   hook points where ``engine/renderer_core.py`` — the brush/mesh/light/portal
   renderer — is driven onto ES. The desktop renderer is coupled to a QOpenGL
   context; the port replaces that context with this one and reuses its geometry
   upload and draw logic. See ``player/README.md`` for the staged plan.

PyOpenGL and numpy are imported lazily so the module stays importable without a
GL binding. Matrix math uses :mod:`player.render.glmath` (numpy) so the mobile
render path has no PyGLM dependency.
"""

from __future__ import annotations

from typing import Dict, Optional

from .gles_context import build_program_set
from .overlay import OverlayRenderer
from . import glmath


class GLESRenderer:
    """Owns GL programs and draws each frame."""

    def __init__(self):
        self.programs: Dict[str, int] = {}
        self.width = 1
        self.height = 1
        self._triangle_vao = None
        self._triangle_vbo = None
        self._scene = None
        self._start_time = 0.0
        self.overlay = OverlayRenderer()

    # ------------------------------------------------------------------
    # GL lifecycle
    # ------------------------------------------------------------------
    def on_gl_ready(self) -> None:
        """(Re)build all GL resources. Safe to call again after context loss."""
        import time

        from ..gles_shaders import build_gles_shader_set, build_gles_shader_map

        gl = _gl()
        self._start_time = time.perf_counter()

        sources = build_gles_shader_set(prefer_arm=True)
        shader_map = build_gles_shader_map()
        self.programs = build_program_set(sources, shader_map)
        print(f"[GLES] compiled {len(self.programs)} shader programs: "
              f"{sorted(self.programs)}")

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glClearColor(0.06, 0.07, 0.09, 1.0)
        self._build_reference_triangle()
        self.overlay.on_gl_ready(self.programs)

        if self._scene is not None:
            self._upload_scene()

    def resize(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        _gl().glViewport(0, 0, self.width, self.height)

    # ------------------------------------------------------------------
    # Reference triangle (milestone 1)
    # ------------------------------------------------------------------
    def _build_reference_triangle(self) -> None:
        import ctypes

        import numpy as np

        gl = _gl()
        verts = np.array(
            [0.0, 0.5, 0.0, -0.5, -0.5, 0.0, 0.5, -0.5, 0.0],
            dtype=np.float32,
        )
        self._triangle_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self._triangle_vao)
        self._triangle_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._triangle_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    def _draw_reference_triangle(self) -> None:
        import time

        gl = _gl()
        prog = self.programs.get("simple")
        if prog is None or self._triangle_vao is None:
            return
        gl.glUseProgram(prog)

        t = time.perf_counter() - self._start_time
        aspect = self.width / max(1, self.height)
        model = glmath.rotate_z(t)
        view = glmath.identity()
        proj = glmath.ortho(-aspect, aspect, -1.0, 1.0, -1.0, 1.0)

        _set_mat4(gl, prog, "model", model)
        _set_mat4(gl, prog, "view", view)
        _set_mat4(gl, prog, "projection", proj)
        _set_vec3(gl, prog, "color", (0.20, 0.85, 0.70))
        _set_float(gl, prog, "alpha", 1.0)

        gl.glBindVertexArray(self._triangle_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glBindVertexArray(0)

    # ------------------------------------------------------------------
    # Per-frame
    # ------------------------------------------------------------------
    def render(self, render_state=None) -> None:
        gl = _gl()
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        if self._scene is not None:
            self.render_scene(render_state)
        else:
            self._draw_reference_triangle()

    def render_overlay(self, controls, inp, w: int, h: int) -> None:
        """Draw the on-screen touch controls over the scene (call last)."""
        self.overlay.render(controls, inp, w, h)

    # ------------------------------------------------------------------
    # Engine bridge seam (Milestone 3+)
    # ------------------------------------------------------------------
    def load_scene(self, map_data: dict, package=None) -> None:
        """Prepare a parsed map for rendering.

        Integration point for the engine geometry pipeline: brush geometry
        (``engine/brush_geometry.py``), models (``engine/glb_loader.py`` /
        ``obj_loader.py``), terrain, lights and portals are built here and their
        GL buffers uploaded via :meth:`_upload_scene`. Left as a documented seam
        until the ES port of ``renderer_core`` lands.
        """
        self._scene = {"map": map_data, "package": package}
        self._upload_scene()

    def _upload_scene(self) -> None:
        # TODO(port): translate engine/renderer_core.py geometry upload to ES.
        pass

    def render_scene(self, render_state) -> None:
        # TODO(port): drive engine/renderer_core.py draw passes on this context.
        self._draw_reference_triangle()


# ----------------------------------------------------------------------
# Small uniform helpers (kept local so the module has no hard GL import)
# ----------------------------------------------------------------------
def _gl():
    import OpenGL.GL as gl
    return gl


def _set_mat4(gl, prog, name, mat) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniformMatrix4fv(loc, 1, gl.GL_TRUE, mat)


def _set_vec3(gl, prog, name, vec) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniform3f(loc, float(vec[0]), float(vec[1]), float(vec[2]))


def _set_float(gl, prog, name, value) -> None:
    loc = gl.glGetUniformLocation(prog, name)
    if loc != -1:
        gl.glUniform1f(loc, float(value))
