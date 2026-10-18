"""Desktop radial point-light depth in a GLES atlas (one sampler)."""
import math
import numpy as np
from .uniforms import location as uniform_location

from . import glmath
from .gles_context import compile_program
from .renderer import _set_mat4
from engine.shaders import MAX_SHADOW_LIGHTS

VERTEX = '''#version 300 es
precision highp float;
layout(location=0) in vec3 position;
uniform mat4 model;
uniform mat4 lightView;
uniform mat4 lightProjection;
out vec3 worldPos;
void main(){worldPos=vec3(model*vec4(position,1.0));gl_Position=lightProjection*lightView*vec4(worldPos,1.0);}
'''
FRAGMENT = '''#version 300 es
precision highp float;
in vec3 worldPos;
uniform vec3 lightPos;
uniform float far_plane;
void main(){gl_FragDepth=length(worldPos-lightPos)/far_plane;}
'''


# Camera visibility must never be used to reject shadow casters.
FACES = (((1,0,0),(0,-1,0)),((-1,0,0),(0,-1,0)),
         ((0,1,0),(0,0,1)),((0,-1,0),(0,0,-1)),
         ((0,0,1),(0,-1,0)),((0,0,-1),(0,-1,0)))


def face_mask(centers, halves, projection_view):
    """Conservative AABB/frustum overlap, including face boundaries."""
    planes = np.asarray([projection_view[3] + sign*projection_view[axis]
                         for axis in range(3) for sign in (-1, 1)])
    return np.all(centers @ planes[:, :3].T + planes[:, 3] +
                  halves @ np.abs(planes[:, :3]).T >= -1e-3, axis=1)


class ShadowAtlas:
    TILE = 384

    def __init__(self, gles=True, tile=384):
        from .renderer import _gl
        from ..gles_shaders import for_desktop
        self.gl = _gl()
        self.TILE = min(tile, int(self.gl.glGetIntegerv(self.gl.GL_MAX_TEXTURE_SIZE))//max(6, MAX_SHADOW_LIGHTS))
        self.program = compile_program(VERTEX if gles else for_desktop(VERTEX), FRAGMENT if gles else for_desktop(FRAGMENT))
        self.texture = self.fbo = self.static_texture = self.static_fbo = None
        self.eye = (0,0,0)
        self.invalidate()

    def invalidate(self):
        self.key = None
        self._static_keys = {}
        self._dynamic_keys = {}
        self.stats = {'shadow_draws': 0, 'shadow_faces_updated': 0,
                      'shadow_static_draws': 0}

    def _allocate(self):
        gl = self.gl
        from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D
        resources = []
        for _ in range(2):
            texture = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_DEPTH_COMPONENT24,6*self.TILE,MAX_SHADOW_LIGHTS*self.TILE,0,gl.GL_DEPTH_COMPONENT,gl.GL_UNSIGNED_INT,None)
            for name,value in ((gl.GL_TEXTURE_MIN_FILTER,gl.GL_NEAREST),(gl.GL_TEXTURE_MAG_FILTER,gl.GL_NEAREST),(gl.GL_TEXTURE_WRAP_S,gl.GL_CLAMP_TO_EDGE),(gl.GL_TEXTURE_WRAP_T,gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_2D,name,value)
            fbo = gl.glGenFramebuffers(1)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER,gl.GL_DEPTH_ATTACHMENT,gl.GL_TEXTURE_2D,texture,0)
            gl.glDrawBuffers(1,[gl.GL_NONE])
            gl.glReadBuffer(gl.GL_NONE)
            if gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)!=gl.GL_FRAMEBUFFER_COMPLETE:
                raise RuntimeError('Point-light shadow framebuffer is incomplete')
            resources.extend((texture, fbo))
        self.texture, self.fbo, self.static_texture, self.static_fbo = resources

    def prepare(self, lights, batches, sources, width, height, bounds=None):
        gl = self.gl
        self.stats = {'shadow_draws': 0, 'shadow_faces_updated': 0,
                      'shadow_static_draws': 0}
        selected = [i for i, light in enumerate(lights) if light.get('casts_shadows')]
        selected.sort(key=lambda i: sum((float(lights[i]['pos'][a])-float(self.eye[a]))**2 for a in range(3)))
        indices = {light: row for row,light in enumerate(selected[:MAX_SHADOW_LIGHTS])}
        if not indices:
            return indices
        casters = []
        for vao, vbo, texture, count in batches:
            source = sources.get(vao)
            model = glmath.identity()
            static = source is None or source[0] == 'static'
            if source and source[0] != 'static':
                kind, item, origin = source
                if kind not in ('brush', 'model'):
                    continue
                if (item.get('hidden') if kind == 'brush' else item.properties.get('hidden') or getattr(item, 'collected', False)):
                    continue
                if kind == 'brush':
                    model = glmath.translate(*(item['pos'][i]-origin[i] for i in range(3)))
                    static = not (item.get('is_mover') or item.get('is_door'))
                else:
                    model = glmath.entity_matrix(item.pos, item.properties)
            center, half = bounds[vao] if bounds is not None else (np.zeros(3), np.full(3, 1e12))
            center = model[:3,:3] @ center + model[:3,3]
            half = np.abs(model[:3,:3]) @ half
            casters.append((vao, count, model, center, half, static))
        centers = np.asarray([c[3] for c in casters]).reshape(-1,3)
        halves = np.asarray([c[4] for c in casters]).reshape(-1,3)
        previous_draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        previous_read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        # Caller starts a world pass with scissoring disabled.
        try:
            if self.texture is None:
                self._allocate()
            gl.glDisable(gl.GL_CULL_FACE)
            gl.glDepthMask(True)
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDisable(gl.GL_BLEND)
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glUseProgram(self.program)
            for index, row in indices.items():
                light = lights[index]
                eye = np.asarray(light['pos'], dtype=float)
                far_plane = max(1., float(light['radius']))
                near = max(far_plane*.002, 1.)
                projection = glmath.perspective(math.pi/2, 1, near, max(far_plane,near+.001))
                influence = np.maximum(np.abs(centers-eye)-halves, 0)
                in_radius = np.sum(influence*influence, axis=1) <= far_plane*far_plane
                gl.glUniform3f(uniform_location(gl,self.program,'lightPos'), *eye)
                gl.glUniform1f(uniform_location(gl,self.program,'far_plane'), far_plane)
                _set_mat4(gl, self.program, 'lightProjection', projection)
                for face, (direction, up) in enumerate(FACES):
                    view = glmath.look_at(eye, eye+direction, up)
                    visible = np.flatnonzero(in_radius & face_mask(centers, halves, projection @ view))
                    static = [casters[i] for i in visible if casters[i][5]]
                    dynamic = [casters[i] for i in visible if not casters[i][5]]
                    identity = (tuple(eye), far_plane)
                    def key(items):
                        return identity, tuple((c[0], c[1], c[2].tobytes()) for c in items)
                    slot = row, face
                    static_key, dynamic_key = key(static), key(dynamic)
                    static_dirty = self._static_keys.get(slot) != static_key
                    if not static_dirty and self._dynamic_keys.get(slot) == dynamic_key:
                        continue
                    x, y, tile = face*self.TILE, row*self.TILE, self.TILE
                    gl.glViewport(x, y, tile, tile)
                    gl.glScissor(x, y, tile, tile)
                    _set_mat4(gl, self.program, 'lightView', view)
                    def draw(items):
                        for vao, count, model, *_ in items:
                            _set_mat4(gl,self.program,'model',model)
                            gl.glBindVertexArray(vao)
                            gl.glDrawArrays(gl.GL_TRIANGLES,0,count)
                            self.stats['shadow_draws'] += 1
                    if static_dirty:
                        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.static_fbo)
                        gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                        draw(static)
                        self.stats['shadow_static_draws'] += len(static)
                    # Copy static depth, then overlay only this face's live casters.
                    gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self.static_fbo)
                    gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.fbo)
                    gl.glBlitFramebuffer(x,y,x+tile,y+tile,x,y,x+tile,y+tile,
                                         gl.GL_DEPTH_BUFFER_BIT,gl.GL_NEAREST)
                    draw(dynamic)
                    self._static_keys[slot] = static_key
                    self._dynamic_keys[slot] = dynamic_key
                    self.stats['shadow_faces_updated'] += 1
            self.key = tuple(sorted(self._dynamic_keys.items()))
        finally:
            gl.glDisable(gl.GL_SCISSOR_TEST)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, previous_read)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, previous_draw)
            gl.glViewport(0,0,width,height)
            gl.glBindVertexArray(0)
        return indices

    def bind(self, program, indices):
        gl = self.gl
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.texture or 0)
        gl.glUniform1i(uniform_location(gl,program,'shadowAtlas'),1)
        gl.glUniform1f(uniform_location(gl,program,'shadowTileSize'),self.TILE)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def close(self):
        gl = self.gl
        for texture in (self.texture, self.static_texture):
            if texture:
                gl.glDeleteTextures([texture])
        for fbo in (self.fbo, self.static_fbo):
            if fbo:
                gl.glDeleteFramebuffers(1,[fbo])
        gl.glDeleteProgram(self.program)
