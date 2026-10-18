"""The desktop std140 light layout, within GLES 3's guaranteed UBO limits."""
import numpy as np
from engine.shaders import MAX_LIGHTS, LIGHT_UBO_BINDING


LIGHT_DTYPE = np.dtype([('position','<f4',4), ('color','<f4',4),
                       ('params','<f4',4), ('indices','<i4',4)])


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
            self.data['position'][row,:3] = light['pos']
            self.data['color'][row,:3] = light['color']
            self.data['params'][row,:2] = light['intensity'], light['radius']
            self.data['indices'][row,0] = shadows.get(row,-1)
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self.buffer)
        gl.glBufferSubData(gl.GL_UNIFORM_BUFFER, 0, self.data.nbytes, self.data)
        gl.glBindBufferBase(gl.GL_UNIFORM_BUFFER, LIGHT_UBO_BINDING, self.buffer)

    def close(self):
        self.gl.glDeleteBuffers(1, [self.buffer])
