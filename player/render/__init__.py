"""OpenGL ES rendering layer for the Fio Player.

This layer bootstraps an ES 3.x pipeline (shader compilation, an offscreen
render target for adaptive resolution) and is the seam where the desktop
``engine/renderer_core.py`` geometry/light/portal code is bridged onto ES.

Milestone 1 ships a minimal, self-checking renderer (clear + a reference
triangle drawn through the translated ``simple`` shader) that proves the whole
chain — context, shader translation, VBO/VAO, draw, present — works on the
device before the heavier engine renderer is wired in.
"""

from .gles_context import (
    GLESContextError,
    compile_program,
    build_program_set,
)

__all__ = ["GLESContextError", "compile_program", "build_program_set"]
