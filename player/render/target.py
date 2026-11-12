"""GLES 3 world framebuffer; HUD and touch controls stay at panel resolution.

The world is drawn into a colour texture and a depth *texture* (not a
renderbuffer), so a pass that reads the scene's depth -- the decals -- can
sample it directly: it draws through :attr:`ScaledTarget.color_fbo`, a second
framebuffer over the same colour texture with no depth attached, so the depth
texture is never both attached and sampled (a feedback loop), and nothing is
copied.
"""
from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D


class ScaledTarget:
    def __init__(self, gl):
        self.gl = gl
        self.fbo = self.texture = self.depth = None
        #: The colour texture alone, for passes that sample :attr:`depth`.
        self.color_fbo = None
        self.size = None

    def begin(self, width, height):
        gl = self.gl
        self.previous_draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        self.previous_read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        if self.fbo is None:
            self.fbo, self.color_fbo = gl.glGenFramebuffers(2)
            self.texture, self.depth = gl.glGenTextures(2)
        if self.size != (width, height):
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
            glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                         gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            for param, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.depth)
            glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH24_STENCIL8, width, height, 0,
                         gl.GL_DEPTH_STENCIL, gl.GL_UNSIGNED_INT_24_8, None)
            # Depth formats are not filterable in ES 3: nearest only.
            for param, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.color_fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self.texture, 0)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Scaled world colour framebuffer is incomplete')
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self.texture, 0)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                      gl.GL_TEXTURE_2D, self.depth, 0)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Scaled world framebuffer is incomplete')
            self.size = (width, height)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        gl.glViewport(0, 0, width, height)

    def present(self, panel_width, panel_height):
        gl = self.gl
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.fbo)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.previous_draw)
        gl.glBlitFramebuffer(0, 0, *self.size, 0, 0, panel_width, panel_height,
                             gl.GL_COLOR_BUFFER_BIT, gl.GL_LINEAR)
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.previous_read)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.previous_draw)
        gl.glViewport(0, 0, panel_width, panel_height)

    def close(self):
        gl = self.gl
        if self.texture:
            gl.glDeleteTextures([self.texture, self.depth])
            gl.glDeleteFramebuffers(2, [self.fbo, self.color_fbo])
        self.fbo = self.color_fbo = self.texture = self.depth = None
        self.size = None
