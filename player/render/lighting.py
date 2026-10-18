"""Compact std140 storage preserves all desktop light values on GLES drivers."""
import numpy as np
from engine.shaders import MAX_LIGHTS, LIGHT_UBO_BINDING


LIGHT_DTYPE = np.dtype([('position_radius','<f4',4), ('color_intensity','<f4',4),
                       ('indices','<i4',4)])


class LightBuffer:
    def __init__(self, gl, program):
        self.gl = gl
        self.data = np.zeros(MAX_LIGHTS, dtype=LIGHT_DTYPE)
        self.buffer = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self.buffer)
        gl.glBufferData(gl.GL_UNIFORM_BUFFER, self.data.nbytes, None, gl.GL_DYNAMIC_DRAW)
        block = gl.glGetUniformBlockIndex(program, 'FioLightBlock')
        gl.glUniformBlockBinding(program, block, LIGHT_UBO_BINDING)

    def upload(self, lights, shadows):
        gl = self.gl
        self.data.fill(0)
        for row, light in enumerate(lights[:MAX_LIGHTS]):
            self.data['position_radius'][row,:3] = light['pos']
            self.data['position_radius'][row,3] = light['radius']
            self.data['color_intensity'][row,:3] = light['color']
            self.data['color_intensity'][row,3] = light['intensity']
            self.data['indices'][row,0] = shadows.get(row,-1)
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self.buffer)
        gl.glBufferSubData(gl.GL_UNIFORM_BUFFER, 0, self.data.nbytes, self.data)
        gl.glBindBufferBase(gl.GL_UNIFORM_BUFFER, LIGHT_UBO_BINDING, self.buffer)

    def close(self):
        self.gl.glDeleteBuffers(1, [self.buffer])
