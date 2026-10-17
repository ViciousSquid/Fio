"""GLES 3 point-light shadows in a depth atlas (one sampler, four lights)."""
import math
import numpy as np
from . import glmath
from .gles_context import compile_program
from .renderer import _set_mat4

VERTEX = '''#version 300 es
precision highp float;
layout(location=0) in vec3 position;
uniform mat4 model;
uniform mat4 lightView;
uniform mat4 lightProjection;
void main(){gl_Position=lightProjection*lightView*model*vec4(position,1.0);}
'''
FRAGMENT = '''#version 300 es
precision highp float;
void main(){}
'''


class ShadowAtlas:
    TILE = 256
    def __init__(self, gles=True):
        from .renderer import _gl
        from ..gles_shaders import for_desktop
        self.gl = _gl()
        self.program = compile_program(VERTEX if gles else for_desktop(VERTEX), FRAGMENT if gles else for_desktop(FRAGMENT))
        self.texture = self.fbo = None
        self.key = None

    def prepare(self, lights, batches, sources, width, height):
        gl = self.gl
        selected = [i for i, light in enumerate(lights) if light.get('casts_shadows')][:4]
        indices = {light: row for row,light in enumerate(selected)}
        if not selected:
            return indices
        if self.texture is None:
            from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D
            self.texture = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.texture)
            glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_DEPTH_COMPONENT24,6*self.TILE,4*self.TILE,0,gl.GL_DEPTH_COMPONENT,gl.GL_UNSIGNED_INT,None)
            for name,value in ((gl.GL_TEXTURE_MIN_FILTER,gl.GL_NEAREST),(gl.GL_TEXTURE_MAG_FILTER,gl.GL_NEAREST),(gl.GL_TEXTURE_WRAP_S,gl.GL_CLAMP_TO_EDGE),(gl.GL_TEXTURE_WRAP_T,gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D,name,value)
            self.fbo=gl.glGenFramebuffers(1)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,gl.GL_DEPTH_ATTACHMENT,gl.GL_TEXTURE_2D,self.texture,0)
            gl.glDrawBuffers(1,[gl.GL_NONE]);gl.glReadBuffer(gl.GL_NONE)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)!=gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Point-light shadow framebuffer is incomplete')
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,0)
        casters=[]
        for vao,vbo,texture,count in batches:
            source=sources.get(vao)
            model=glmath.identity()
            if source:
                kind,item,origin=source
                if kind not in ('brush','model'):
                    continue
                hidden=item.get('hidden') if kind=='brush' else item.properties.get('hidden') or getattr(item,'collected',False)
                if hidden:
                    continue
                model=glmath.translate(*(item['pos'][i]-origin[i] for i in range(3))) if kind=='brush' else glmath.entity_matrix(item.pos,item.properties)
            casters.append((vao,count,model))
        key=(tuple((i,tuple(lights[i]['pos']),lights[i]['radius']) for i in selected),
             tuple((vao,model.tobytes()) for vao,count,model in casters))
        if key!=self.key:
            previous=int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
            try:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,self.fbo)
                gl.glDisable(gl.GL_CULL_FACE)
                gl.glDepthMask(True);gl.glEnable(gl.GL_DEPTH_TEST);gl.glDisable(gl.GL_BLEND)
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                gl.glUseProgram(self.program)
                faces=(((1,0,0),(0,-1,0)),((-1,0,0),(0,-1,0)),((0,1,0),(0,0,1)),((0,-1,0),(0,0,-1)),((0,0,1),(0,-1,0)),((0,0,-1),(0,-1,0)))
                for index,row in indices.items():
                    light=lights[index];eye=np.asarray(light['pos'],dtype=float)
                    _set_mat4(gl,self.program,'lightProjection',glmath.perspective(math.pi/2,1,.5,max(1,light['radius'])))
                    for face,(direction,up) in enumerate(faces):
                        gl.glViewport(face*self.TILE,row*self.TILE,self.TILE,self.TILE)
                        _set_mat4(gl,self.program,'lightView',glmath.look_at(eye,eye+direction,up))
                        for vao,count,model in casters:
                            _set_mat4(gl,self.program,'model',model)
                            gl.glBindVertexArray(vao);gl.glDrawArrays(gl.GL_TRIANGLES,0,count)
                self.key=key
            finally:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER,previous)
                gl.glViewport(0,0,width,height)
                gl.glBindVertexArray(0)
        return indices

    def bind(self, program, indices):
        gl=self.gl
        gl.glActiveTexture(gl.GL_TEXTURE1);gl.glBindTexture(gl.GL_TEXTURE_2D,self.texture or 0)
        gl.glUniform1i(gl.glGetUniformLocation(program,'shadowAtlas'),1)
        for index in range(32):
            gl.glUniform1i(gl.glGetUniformLocation(program,f'lightShadow[{index}]'),indices.get(index,-1))
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def close(self):
        gl=self.gl
        if self.texture:gl.glDeleteTextures([self.texture])
        if self.fbo:gl.glDeleteFramebuffers(1,[self.fbo])
        gl.glDeleteProgram(self.program)
