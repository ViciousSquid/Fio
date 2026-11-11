"""Projected decals: the pass that paints the published decal boxes.

The frame input's ``decals`` is two arrays from the logic thread's
``DecalTable`` -- box matrices and ``(variant, opacity, kind, mirrored)`` -- and nothing
else: no actor, no brush. Each box is drawn as an instance of the unit cube;
the fragment shader reads the scene depth under each pixel, puts that
surface point in the box's space and paints the decal's texture where it
lies inside (``decal.frag``). The pass runs after the opaque world is drawn
and before anything blended, so decals lie on walls, floors, terrain and
models, and under water and glass.

Its GL objects are the renderer's: the program, the instance buffer, a
vertex array over the shared cube, and one texture array holding every
``assets/textures/decals/blood_wall_*.png`` and ``blood_floor_*.png``.
"""

import ctypes
import os

import glm
import numpy as np
import OpenGL.GL as gl

from engine import shaders
from engine.asset_archives import asset_listdir, asset_source

DECAL_FOLDER = os.path.join('assets', 'textures', 'decals')
#: Side of a layer of the decal texture array; images are scaled to it.
DECAL_TEXTURE_SIZE = 256
#: Floats an instance: a 4x4 box matrix and (variant, opacity, 0, 0).
_INSTANCE_FLOATS = 20


class DecalsMixin:
    """``draw_decals`` and the GL objects it owns."""

    def _init_decals(self):
        self._decal_vao = 0
        self._decal_vbo = 0
        self._decal_capacity = 0
        self._decal_textures = 0
        self._decal_layers = 0
        self._decal_ranges = (0.0, 1.0, 0.0, 1.0)
        self._decal_staging = np.zeros((0, _INSTANCE_FLOATS), dtype=np.float32)
        #: Decals drawn by the last frame (tests, Debug Tables).
        self.decals_drawn = 0

    def _decal_program(self):
        program = self.shaders.get('decal')
        if program is None:
            try:
                program = self.shader_loader.compile_from_source(
                    shaders.DEFAULT_SHADERS['decal.vert'],
                    shaders.DEFAULT_SHADERS['decal.frag'])
            except Exception as exc:
                print(f'[Renderer] projected decals disabled: {exc}')
                program = 0
            self.shaders['decal'] = program
            if program:
                self._decal_uniforms = {
                    name: gl.glGetUniformLocation(program, name)
                    for name in ('projection', 'view', 'sceneDepth', 'decalTextures',
                                 'decalRanges', 'invViewProjection', 'viewport', 'viewPos')}
        return program

    def _decal_texture_array(self):
        """Every decal image as one texture array (made on first use)."""
        if self._decal_textures or self._decal_layers < 0:
            return self._decal_textures
        from PIL import Image
        found = sorted(name for name in asset_listdir(DECAL_FOLDER)
                       if name.lower().startswith('blood_') and name.lower().endswith('.png'))
        # Wall images, then floor images. With only one kind (or files named
        # neither), both kinds draw from all of them.
        walls = [n for n in found if n.lower().startswith('blood_wall_')]
        floors = [n for n in found if n.lower().startswith('blood_floor_')]
        if not walls or not floors:
            walls = floors = found
            names = found
            self._decal_ranges = (0.0, float(len(found)), 0.0, float(len(found)))
        else:
            names = walls + floors
            self._decal_ranges = (0.0, float(len(walls)),
                                  float(len(walls)), float(len(floors)))
        layers = []
        size = DECAL_TEXTURE_SIZE
        for name in names:
            try:
                image = Image.open(asset_source(os.path.join(DECAL_FOLDER, name)))
                layers.append(np.asarray(
                    image.convert('RGBA').resize((size, size), Image.LANCZOS), dtype=np.uint8))
            except Exception as exc:
                print(f'[Renderer] decal texture {name} not loaded: {exc}')
        if not layers:
            self._decal_layers = -1                 # none: asked once
            return 0
        data = np.ascontiguousarray(np.stack(layers))
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_RGBA8, size, size, len(layers), 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D_ARRAY)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        self._decal_textures, self._decal_layers = texture, len(layers)
        return texture

    def _decal_instances(self, count):
        """The vertex array (the shared cube, plus per-instance box and
        params) with room for *count* instances, bound."""
        if not self._decal_vao:
            self._decal_vao = int(gl.glGenVertexArrays(1))
            self._decal_vbo = int(gl.glGenBuffers(1))
            gl.glBindVertexArray(self._decal_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._cube_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 8 * 4, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
            stride = _INSTANCE_FLOATS * 4
            for i in range(5):
                gl.glVertexAttribPointer(3 + i, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                         ctypes.c_void_p(i * 16))
                gl.glEnableVertexAttribArray(3 + i)
                gl.glVertexAttribDivisor(3 + i, 1)
        gl.glBindVertexArray(self._decal_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
        if count > self._decal_capacity:
            self._decal_capacity = max(64, count, self._decal_capacity * 2)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, self._decal_capacity * _INSTANCE_FLOATS * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            self._decal_staging = np.zeros((self._decal_capacity, _INSTANCE_FLOATS),
                                           dtype=np.float32)

    def draw_decals(self, projection, view, camera_pos, decals):
        """Paint the published *decals* -- ``(models (n, 16), params (n, 4))``
        float32 -- onto what the frame has drawn so far."""
        self.decals_drawn = 0
        if decals is None:
            return 0
        models, params = decals
        count = len(models)
        if not count or not self._cube_vbo:
            return 0
        program = self._decal_program()
        if not program or not self._decal_texture_array():
            return 0
        # The depth the boxes are tested against is a copy: the pass reads
        # it while the framebuffer's own depth stays attached.
        if not self._capture_scene_depth():
            return 0
        viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        u = self._decal_uniforms
        gl.glUseProgram(program)
        self._current_shader = program
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        inverse = glm.inverse(projection * view)        # kept alive for value_ptr
        gl.glUniformMatrix4fv(u['invViewProjection'], 1, gl.GL_FALSE, glm.value_ptr(inverse))
        gl.glUniform4f(u['viewport'], float(viewport[0]), float(viewport[1]),
                       float(max(int(viewport[2]), 1)), float(max(int(viewport[3]), 1)))
        gl.glUniform3f(u['viewPos'], float(camera_pos.x), float(camera_pos.y),
                       float(camera_pos.z))
        gl.glUniform1i(u['sceneDepth'], self._water_depth_texture_unit)
        gl.glUniform4f(u['decalRanges'], *self._decal_ranges)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
        gl.glUniform1i(u['decalTextures'], 0)

        self._decal_instances(count)
        staging = self._decal_staging[:count]
        staging[:, 0:16] = models
        staging[:, 16:20] = params
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, staging.nbytes, staging)

        # The box's far faces, untested against depth: every pixel the box
        # covers is reached once, the camera inside the box or not. The
        # shader does the real test, against the copy.
        blend = bool(gl.glIsEnabled(gl.GL_BLEND))
        cull = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        depth_test = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        depth_write = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_DST_COLOR, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_FRONT)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        try:
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, count)
        finally:
            gl.glCullFace(gl.GL_BACK)
            if not cull:
                gl.glDisable(gl.GL_CULL_FACE)
            if depth_test:
                gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthMask(gl.GL_TRUE if depth_write else gl.GL_FALSE)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            if not blend:
                gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        self.render_stats.draw_calls += 1
        self.decals_drawn = count
        return count

    def _cleanup_decals(self):
        if self._decal_vao:
            gl.glDeleteVertexArrays(1, [self._decal_vao])
        if self._decal_vbo:
            gl.glDeleteBuffers(1, [self._decal_vbo])
        if self._decal_textures:
            gl.glDeleteTextures(1, [self._decal_textures])
        self._decal_vao = self._decal_vbo = self._decal_textures = 0
        self._decal_capacity = 0
        self._decal_layers = 0
