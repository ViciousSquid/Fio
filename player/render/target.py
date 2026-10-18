"""GLES 3 world framebuffer; HUD and touch controls stay at panel resolution."""
from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D


class ScaledTarget:
    def __init__(self, gl):
        self.gl = gl
        self.fbo = self.texture = self.depth = None
        self.size = None

    def begin(self, width, height):
        gl = self.gl
        self.previous_draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        self.previous_read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        if self.fbo is None:
            self.fbo = gl.glGenFramebuffers(1)
            self.texture = gl.glGenTextures(1)
            self.depth = gl.glGenRenderbuffers(1)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.fbo)
        if self.size != (width, height):
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
            glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                         gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            for param, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                 (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                 (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, param, value)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self.texture, 0)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self.depth)
            gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, width, height)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                         gl.GL_RENDERBUFFER, self.depth)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER) != gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Scaled world framebuffer is incomplete')
            self.size = (width, height)
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
            gl.glDeleteTextures([self.texture])
            gl.glDeleteRenderbuffers(1, [self.depth])
            gl.glDeleteFramebuffers(1, [self.fbo])
        self.fbo = self.texture = self.depth = None
        self.size = None
