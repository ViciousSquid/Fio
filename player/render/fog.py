"""Local fog volumes with a player-specific ray-march budget."""
import ctypes
from pathlib import Path
import numpy as np
from engine.render_table import RenderTable, model_matrices
from engine.shaders import DEFAULT_SHADERS
from player.gles_shaders import translate, for_desktop
from .uniforms import location as uniform_location

from .gles_context import compile_program
from .scene import build_brush_mesh
from .renderer import _set_mat4, _set_float, _set_vec3


class FogVolumes:
    def __init__(self, gl, gles=True, steps=16):
        self.gl = gl
        convert = translate if gles else lambda source, stage: for_desktop(source)
        fragment = DEFAULT_SHADERS['fog_arm.frag'].replace('int   num_steps = 16;',
                                                        f'int   num_steps = {steps};')
        self.program = compile_program(convert(DEFAULT_SHADERS['fog.vert'],'vertex'),
                                       convert(fragment,'fragment'))
        vertices, self.count = build_brush_mesh({'brushes':[{'pos':[0,0,0],'size':[1,1,1]}]})
        self.vao, self.vbo = gl.glGenVertexArrays(1), gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao);gl.glBindBuffer(gl.GL_ARRAY_BUFFER,self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER,vertices.nbytes,vertices,gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0,3,gl.GL_FLOAT,gl.GL_FALSE,24,ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0);gl.glBindVertexArray(0)
        raw = (Path(__file__).parents[1]/'runtime/noise_3d.bin').read_bytes()
        if len(raw) != 32**3:
            raise ValueError('Invalid shared fog noise texture')
        self.noise = gl.glGenTextures(1);gl.glBindTexture(gl.GL_TEXTURE_3D,self.noise)
        from OpenGL.raw.GL.VERSION.GL_1_2 import glTexImage3D
        data = ctypes.create_string_buffer(raw)
        glTexImage3D(gl.GL_TEXTURE_3D,0,gl.GL_R8,32,32,32,0,gl.GL_RED,gl.GL_UNSIGNED_BYTE,ctypes.cast(data,ctypes.c_void_p))
        for param in (gl.GL_TEXTURE_WRAP_S,gl.GL_TEXTURE_WRAP_T,gl.GL_TEXTURE_WRAP_R):
            gl.glTexParameteri(gl.GL_TEXTURE_3D,param,gl.GL_REPEAT)
        for param in (gl.GL_TEXTURE_MIN_FILTER,gl.GL_TEXTURE_MAG_FILTER):
            gl.glTexParameteri(gl.GL_TEXTURE_3D,param,gl.GL_LINEAR)
        self.table = RenderTable()
        self.epoch = 0

    def render(self, brushes, view, projection, eye, visibility, elapsed):
        brushes = [b for b in brushes if (b.get('is_fog') or b.get('shader')=='Fog') and not b.get('hidden')]
        if not brushes:
            return 0
        self.epoch += 1
        self.table.begin_frame(brushes,epoch=self.epoch)
        slots = np.arange(self.table.count,dtype=np.int32)
        matrices, _ = model_matrices(self.table,slots)
        gl = self.gl;prog = self.program
        gl.glUseProgram(prog)
        _set_mat4(gl,prog,'view',view);_set_mat4(gl,prog,'projection',projection)
        _set_vec3(gl,prog,'viewPos',eye);_set_float(gl,prog,'time',elapsed)
        gl.glActiveTexture(gl.GL_TEXTURE1);gl.glBindTexture(gl.GL_TEXTURE_3D,self.noise)
        gl.glUniform1i(uniform_location(gl, prog,'noiseTexture'),1)
        gl.glEnable(gl.GL_BLEND);gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE);gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glPolygonOffset(-1.,-1.);gl.glBindVertexArray(self.vao)
        drawn = 0
        try:
            for row, matrix in enumerate(matrices):
                model = matrix.reshape(4,4).T
                if not visibility.visible((np.zeros(3),np.full(3,.5)),model,self.table.center[row]):
                    continue
                _set_mat4(gl,prog,'model',model)
                _set_mat4(gl,prog,'inverseModel',np.linalg.inv(model))
                _set_vec3(gl,prog,'fogColor',self.table.fog_color[row])
                _set_float(gl,prog,'density',self.table.fog_params[row,0])
                _set_float(gl,prog,'noiseScale',self.table.fog_params[row,1])
                # Player cubes wind outwards; the desktop cube winds inwards.
                for face in (gl.GL_BACK,gl.GL_FRONT):
                    gl.glCullFace(face);gl.glDrawArrays(gl.GL_TRIANGLES,0,self.count)
                drawn += 1
        finally:
            gl.glDisable(gl.GL_POLYGON_OFFSET_FILL);gl.glDisable(gl.GL_CULL_FACE)
            gl.glDisable(gl.GL_BLEND);gl.glBindVertexArray(0);gl.glActiveTexture(gl.GL_TEXTURE0)
        return drawn

    def close(self):
        gl = self.gl
        gl.glDeleteTextures([self.noise]);gl.glDeleteBuffers(1,[self.vbo])
        gl.glDeleteVertexArrays(1,[self.vao]);gl.glDeleteProgram(self.program)
