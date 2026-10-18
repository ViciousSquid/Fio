"""Player water pass: shared cheap water optics, bounded meshes, one scene copy.

Depth absorption, caustics and screen-space reflections are compiled out. The
shared shader still supplies waves, normal detail, refraction, sky Fresnel,
point lights, authored tint/opacity and radial fog at the world target size.
"""
import ctypes
from pathlib import Path

import numpy as np

from engine.constants import is_water_brush, normalize_color
from engine.shaders import DEFAULT_SHADERS
from player.gles_shaders import translate, for_desktop
from . import glmath
from .gles_context import compile_program
from .lighting import batch_lights
from .scene import _FACES
from .uniforms import location


def water_mesh(brush, subdivisions=8):
    """Local pos3/normal3/uv2 mesh; editor textures never enter this pass."""
    vertices = []
    if brush.get('geometry', {}).get('planes'):
        from engine.brush_geometry import get_convex
        convex = get_convex(brush)
        size = np.maximum(np.abs(brush['size']), .001)
        for face in convex.faces:
            points = (convex.verts[face['indices']] - brush['pos']) / size
            normal = np.asarray(face['normal'])
            if brush.get('water_plane') and normal[1] <= 0:
                continue
            local_normal = normal * size
            local_normal /= np.linalg.norm(local_normal)
            for i in range(1, len(points)-1):
                for point in points[[0,i,i+1]]:
                    vertices.append((*point, *local_normal, point[0]+.5, point[2]+.5))
    else:
        if not brush.get('water_plane'):
            for normal, corners in _FACES[:4]:
                for index in (0,1,2,0,2,3):
                    point = corners[index]
                    vertices.append((*point,*normal,point[0]+.5,point[1]+.5))
        # A small grid gives Gerstner waves interior vertices to displace.
        for z in range(subdivisions):
            for x in range(subdivisions):
                for dx,dz in ((0,0),(0,1),(1,1),(0,0),(1,1),(1,0)):
                    u,v = (x+dx)/subdivisions, (z+dz)/subdivisions
                    vertices.append((u-.5,.5,v-.5,0,1,0,u,v))
    return np.asarray(vertices,dtype=np.float32).reshape(-1,8)


class WaterSurfaces:
    def __init__(self, gl, gles=True, subdivisions=8):
        self.gl, self.gles, self.subdivisions = gl, gles, subdivisions
        self.program = self.normal = self.scene = None
        self.scene_size = None
        self.meshes = []

    def load(self, brushes):
        self.clear()
        gl = self.gl
        for brush in brushes:
            if not is_water_brush(brush):
                continue
            vertices = water_mesh(brush,self.subdivisions)
            if not len(vertices):
                continue
            vao, vbo = gl.glGenVertexArrays(1), gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER,vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER,vertices.nbytes,vertices,gl.GL_STATIC_DRAW)
            for attribute,size,offset in ((0,3,0),(1,3,12),(2,2,24)):
                gl.glVertexAttribPointer(attribute,size,gl.GL_FLOAT,gl.GL_FALSE,32,ctypes.c_void_p(offset))
                gl.glEnableVertexAttribArray(attribute)
            low,high = vertices[:,:3].min(0),vertices[:,:3].max(0)
            self.meshes.append((brush,vao,vbo,len(vertices),((low+high)*.5,(high-low)*.5)))
        gl.glBindVertexArray(0)

    def _prepare(self):
        from PIL import Image
        from .assets import upload_rgba
        gl = self.gl
        convert = translate if self.gles else lambda source, stage: for_desktop(source)
        fragment = DEFAULT_SHADERS['water.frag']
        for name in ('hasSceneDepth','ssrEnabled'):
            fragment = fragment.replace(f'uniform int {name};',f'const int {name} = 0;')
        self.program = compile_program(convert(DEFAULT_SHADERS['water.vert'],'vertex'),
                                       convert(fragment,'fragment'))
        self.normal = gl.glGenTextures(1)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.normal)
        with Image.open(Path(__file__).parents[1]/'runtime/water_normal.png') as image:
            upload_rgba(gl,image.convert('RGBA').transpose(Image.Transpose.FLIP_TOP_BOTTOM))
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MIN_FILTER,gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D,gl.GL_TEXTURE_MAG_FILTER,gl.GL_LINEAR)
        for param in (gl.GL_TEXTURE_WRAP_S,gl.GL_TEXTURE_WRAP_T):
            gl.glTexParameteri(gl.GL_TEXTURE_2D,param,gl.GL_REPEAT)
        self.scene = gl.glGenTextures(1)
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.scene)
        for param in (gl.GL_TEXTURE_MIN_FILTER,gl.GL_TEXTURE_MAG_FILTER):
            gl.glTexParameteri(gl.GL_TEXTURE_2D,param,gl.GL_LINEAR)
        for param in (gl.GL_TEXTURE_WRAP_S,gl.GL_TEXTURE_WRAP_T):
            gl.glTexParameteri(gl.GL_TEXTURE_2D,param,gl.GL_CLAMP_TO_EDGE)

    def render(self, view, projection, eye, visibility, env, lights, elapsed, width, height):
        from .renderer import _set_mat4, _set_vec3, _set_float, _set_environment
        visible = []
        for brush,vao,vbo,count,(center,half) in self.meshes:
            if brush.get('hidden'):
                continue
            size = np.maximum(np.abs(brush['size']),.001)
            model = glmath.translate(*brush['pos']) @ glmath.scale(*size)
            # Conservative room for the shared shader's bounded wave displacement.
            if visibility.visible((center,half+60/size),model,brush['pos']):
                visible.append((brush,vao,count,size,model))
        if not visible:
            return 0
        gl = self.gl
        if self.program is None:
            self._prepare()
        prog = self.program
        gl.glUseProgram(prog)
        _set_mat4(gl,prog,'view',view)
        _set_mat4(gl,prog,'projection',projection)
        _set_vec3(gl,prog,'viewPos',eye)
        _set_float(gl,prog,'time',elapsed)
        _set_environment(gl,prog,env)
        gl.glUniform1i(location(gl,prog,'normalMap'),0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.normal)
        gl.glUniform1i(location(gl,prog,'sceneColor'),1)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D,self.scene)
        if self.scene_size != (width,height):
            from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D
            glTexImage2D(gl.GL_TEXTURE_2D,0,gl.GL_RGBA8,width,height,0,
                         gl.GL_RGBA,gl.GL_UNSIGNED_BYTE,None)
            self.scene_size = (width,height)
        # Read from the current world target, including adaptive-resolution FBOs.
        previous_read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)))
        try:
            gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D,0,0,0,0,0,width,height)
        finally:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,previous_read)
        gl.glUniform2f(location(gl,prog,'screenSize'),width,height)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(True)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glDisable(gl.GL_CULL_FACE)  # Surface remains visible while swimming.
        try:
            for brush,vao,count,size,model in sorted(visible,
                    key=lambda item: -float(np.sum((np.asarray(item[0]['pos'])-eye)**2))):
                _set_mat4(gl,prog,'model',model)
                normal = np.diag(1/size).astype(np.float32)
                gl.glUniformMatrix3fv(location(gl,prog,'normalMatrix'),1,gl.GL_FALSE,
                                      np.ascontiguousarray(normal.T))
                _set_vec3(gl,prog,'brushSize',size)
                _set_vec3(gl,prog,'waterTint',normalize_color(brush.get('water_tint',[0,.4,.6])))
                fresnel = float(brush.get('water_fresnel',brush.get('water_reflectivity',.65)))
                for name,value in (('waterOpacity',brush.get('water_opacity',.5)),
                                   ('waterReflectivity',fresnel),('fresnelIntensity',fresnel),
                                   ('distortionStrength',brush.get('water_distortion',.5)),
                                   ('refractionIndex',max(1,float(brush.get('water_refraction',1.333)))),
                                   ('roughness',np.clip(brush.get('water_roughness',0),0,1))):
                    _set_float(gl,prog,name,float(value))
                h = float(brush.get('water_wave_height',.5))
                if h > 2:
                    h /= 100
                amp = h*30 if brush.get('water_wave_enabled',True) else 1.2
                _set_float(gl,prog,'waveAmp',min(amp,float(size[1])*.45,30))
                chosen = batch_lights(lights,np.asarray(brush['pos']),size*.5,8)
                gl.glUniform1i(location(gl,prog,'active_lights'),len(chosen))
                for index,light_index in enumerate(chosen):
                    light = lights[light_index]
                    prefix = f'lights[{index}]'
                    _set_vec3(gl,prog,prefix+'.position',light['pos'])
                    _set_vec3(gl,prog,prefix+'.color',light['color'])
                    _set_float(gl,prog,prefix+'.intensity',light['intensity'])
                    _set_float(gl,prog,prefix+'.radius',light['radius'])
                gl.glBindVertexArray(vao)
                gl.glDrawArrays(gl.GL_TRIANGLES,0,count)
        finally:
            gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
        return len(visible)

    def clear(self):
        gl = self.gl
        for _,vao,vbo,_,_ in self.meshes:
            gl.glDeleteVertexArrays(1,[vao])
            gl.glDeleteBuffers(1,[vbo])
        self.meshes.clear()

    def close(self):
        self.clear()
        gl = self.gl
        if self.program:
            gl.glDeleteProgram(self.program)
            gl.glDeleteTextures([self.normal,self.scene])
        self.program = self.normal = self.scene = None
