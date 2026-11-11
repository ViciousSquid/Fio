"""The engine's terrain GL, with its programs compiled for this host's GL.

The engine publishes the terrain as an immutable ``TerrainFrame``
(``Terrain.advance`` then ``Terrain.publish``; ``engine/terrain_frame.py``)
and ``engine.renderer.core.terrain_gl.TerrainGL`` draws one: it owns the
programs, height and paint pages, index arrays and grass and block buffers.
The player draws through the same class. The one difference is how a program
is built: the desktop compiles GLSL 3.30 with the shared light block, the
player hands the same sources to its own compiler (GLSL ES 3.00 on a device,
desktop GLSL on a development host), without the light block it never fills.
"""
from __future__ import annotations

import engine.renderer.core.terrain_gl as _engine_terrain_gl
from engine import shaders
from engine.renderer.core.terrain_gl import TerrainGL


def fragment_for(vertex_name):
    """The fragment shader ``TerrainGL._compile_program`` pairs with
    *vertex_name*: the heightfield draws flat face outputs, the block mesh
    smooth ones. (player/tests/test_scene.py holds the two to each other.)"""
    return 'terrain_flat.frag' if vertex_name == 'terrain_indexed.vert' else 'terrain.frag'


class PlayerTerrainGL(TerrainGL):
    """``TerrainGL`` whose programs come from ``compiler(vertex, fragment)``."""

    def __init__(self, compiler):
        super().__init__()
        self._compiler = compiler

    def _compile_program(self, vertex_name):
        if vertex_name in self._failed_programs:
            return 0
        try:
            program = self._compiler(shaders.DEFAULT_SHADERS[vertex_name],
                                     shaders.DEFAULT_SHADERS[fragment_for(vertex_name)])
        except Exception as exc:
            print(f"ERROR: Exception during terrain shader compilation: {exc}")
            program = 0
        if not program:
            self._failed_programs.add(vertex_name)
            return 0
        return program

    def _init_grass_shader(self):
        # The engine method compiles and then looks up the grass uniforms;
        # only the compile differs here, so it runs unchanged with the
        # module's two compile calls routed to this host's compiler.
        module = _engine_terrain_gl
        saved = module.compileShader, module.compileProgram
        module.compileShader = lambda source, _kind: source
        module.compileProgram = (
            lambda vertex, fragment, validate=False: self._compiler(vertex, fragment))
        try:
            super()._init_grass_shader()
        finally:
            module.compileShader, module.compileProgram = saved
