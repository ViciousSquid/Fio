"""Projected decals: the pass that paints the published decal boxes.

The frame input's ``decals`` is three arrays from the logic thread's
``DecalTable`` -- box matrices, ``(variant, opacity, kind, flags)`` and the
boxes' inverses -- and nothing else: no actor, no brush. Each box is drawn
as an instance of the unit cube;
the fragment shader reads the scene depth under each pixel, puts that
surface point in the box's space and paints the decal's texture where it
lies inside (``decal.frag``). The pass runs after the opaque world is drawn
and before anything blended, so decals lie on walls, floors, terrain and
models, and under water and glass.

Its GL objects are the renderer's: the program, the instance buffer, a
vertex array over the shared cube, and one texture array holding every image
in ``assets/textures/decals``, in ``engine.decal_textures`` order (blood
picks among the ``blood_wall_*`` / ``blood_floor_*`` layers; a placed decal
names its own layer).

Placed wet decals are rows of the same table, drawn by a second draw of the
same program over a copy of the frame so far: darkened, and reflecting the
scene by the water's screen-space reflection, faded out with distance (see
``engine.shaders.REFLECTION_FADE_START``). ``reflections_enabled`` (the
``reflections`` console command) turns that off with every other one.

Only decals that can show are drawn (:func:`visible_decals`): a box wholly
outside the view, or so far off it would cover less than half a pixel, is
left out before anything is uploaded -- and with none left, the pass costs
nothing, not even its depth copy. The image and blood decals that remain
are one instanced draw, the wet ones one more, whatever their number.
"""

import ctypes
import os

import glm
import numpy as np
import OpenGL.GL as gl

from engine import shaders
from engine import decal_textures
from engine.decal_table import box_inverse
from engine.asset_archives import asset_source

DECAL_FOLDER = os.path.join('assets', 'textures', 'decals')
#: Side of a layer of the decal texture array; images are scaled to it.
DECAL_TEXTURE_SIZE = 256
#: Floats an instance: a 4x4 box matrix, (variant, opacity, kind, flags)
#: and the box's inverse.
_INSTANCE_FLOATS = 36
#: vec4 attributes an instance: four model columns, params, four inverse
#: columns -- locations 3..11.
_INSTANCE_VEC4S = _INSTANCE_FLOATS // 4
#: A decal covering less than this many pixels across (its radius, on
#: screen) is not drawn.
MIN_PIXEL_RADIUS = 0.5


def visible_decals(models, projection, view, camera_pos, viewport_height):
    """Indices (ascending) of the boxes in *models* (n, 16) that can show:
    their bounding spheres touch the view frustum and are at least
    :data:`MIN_PIXEL_RADIUS` across on screen. Vectorised: a few array
    operations, whatever the count."""
    models = np.asarray(models, dtype=np.float32)
    if not len(models):
        return np.zeros(0, dtype=np.intp)
    centres = models[:, 12:15].astype(np.float64)
    # Half the box's diagonal: its columns are its three edges.
    radius = 0.5 * np.sqrt((models[:, 0:3] ** 2).sum(1) + (models[:, 4:7] ** 2).sum(1)
                           + (models[:, 8:11] ** 2).sum(1))
    # Gribb-Hartmann: the frustum's planes from the rows of projection*view.
    m = np.array((projection * view).to_list(), dtype=np.float64).T
    planes = np.stack((m[3] + m[0], m[3] - m[0], m[3] + m[1],
                       m[3] - m[1], m[3] + m[2], m[3] - m[2]))
    planes /= np.maximum(np.linalg.norm(planes[:, :3], axis=1), 1e-12)[:, None]
    inside = (centres @ planes[:, :3].T + planes[:, 3] >= -radius[:, None]).all(1)
    # Too small to see: radius over distance, in pixels.
    eye = np.array((float(camera_pos.x), float(camera_pos.y), float(camera_pos.z)))
    distance = np.linalg.norm(centres - eye, axis=1)
    pixels = radius * abs(float(projection[1][1])) * 0.5 * float(viewport_height)
    big_enough = (distance <= radius) | (pixels >= MIN_PIXEL_RADIUS * distance)
    return np.flatnonzero(inside & big_enough)


class DecalsMixin:
    """``draw_decals`` and the GL objects it owns."""

    def _init_decals(self):
        self._decal_vao = 0
        self._decal_vbo = 0
        self._decal_capacity = 0
        self._decal_textures = 0
        self._decal_layers = 0
        self._decal_images_version = None
        self._decal_ranges = (0.0, 1.0, 0.0, 1.0)
        self._decal_staging = np.zeros((0, _INSTANCE_FLOATS), dtype=np.float32)
        self._decal_base = None
        #: Decals drawn by the last frame, and those left out as off screen
        #: or too small (tests, Debug Tables).
        self.decals_drawn = 0
        self.decals_culled = 0

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
                                 'decalRanges', 'invViewProjection', 'viewport', 'viewPos',
                                 'wetPass', 'reflectionsOn', 'sceneColor', 'invProjection',
                                 'decalFadeEnd')}
        return program

    def _decal_texture_array(self):
        """Every decal image as one texture array, in
        ``engine.decal_textures`` order; rebuilt when that list changes."""
        version, names = decal_textures.images()
        if version == self._decal_images_version:
            return self._decal_textures
        from PIL import Image
        if self._decal_textures:
            gl.glDeleteTextures(1, [self._decal_textures])
            self._decal_textures = 0
        self._decal_images_version = version
        self._decal_ranges = decal_textures.blood_ranges(names)
        layers = []
        size = DECAL_TEXTURE_SIZE
        blank = np.zeros((size, size, 4), dtype=np.uint8)
        for name in names:
            try:
                image = Image.open(asset_source(os.path.join(DECAL_FOLDER, name)))
                layers.append(np.asarray(
                    image.convert('RGBA').resize((size, size), Image.LANCZOS), dtype=np.uint8))
            except Exception as exc:
                # Kept as a blank layer: every later image keeps its layer.
                print(f'[Renderer] decal texture {name} not loaded: {exc}')
                layers.append(blank)
        if not layers:
            self._decal_layers = 0
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
        """The vertex array (the shared cube, plus per-instance box, params
        and inverse) with room for *count* instances, bound."""
        if not self._decal_vao:
            self._decal_vao = int(gl.glGenVertexArrays(1))
            self._decal_vbo = int(gl.glGenBuffers(1))
            gl.glBindVertexArray(self._decal_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._cube_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 8 * 4, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
            for i in range(_INSTANCE_VEC4S):
                gl.glEnableVertexAttribArray(3 + i)
                gl.glVertexAttribDivisor(3 + i, 1)
            self._decal_base = None
        gl.glBindVertexArray(self._decal_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
        if count > self._decal_capacity:
            self._decal_capacity = max(64, count, self._decal_capacity * 2)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, self._decal_capacity * _INSTANCE_FLOATS * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            self._decal_staging = np.zeros((self._decal_capacity, _INSTANCE_FLOATS),
                                           dtype=np.float32)

    def _decal_point(self, base):
        """Point the instance attributes at instance *base* onward (the
        vertex array and its buffer bound): the wet decals follow the
        others in the one upload."""
        if base == self._decal_base:
            return
        stride = _INSTANCE_FLOATS * 4
        for i in range(_INSTANCE_VEC4S):
            gl.glVertexAttribPointer(3 + i, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(base * stride + i * 16))
        self._decal_base = base

    def draw_decals(self, projection, view, camera_pos, decals):
        """Paint the published *decals* -- ``(models (n, 16), params (n, 4),
        inverses (n, 16))`` float32 (without the inverses, they are worked
        out here) -- onto what the frame has drawn so far."""
        self.decals_drawn = 0
        self.decals_culled = 0
        if decals is None:
            return 0
        models, params = decals[0], decals[1]
        if not len(models) or not self._cube_vbo:
            return 0
        viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        keep = visible_decals(models, projection, view, camera_pos, int(viewport[3]))
        self.decals_culled = len(models) - len(keep)
        count = len(keep)
        if not count:
            return 0
        program = self._decal_program()
        if not program or not self._decal_texture_array():
            return 0
        # The depth the boxes are tested against is a copy: the pass reads
        # it while the framebuffer's own depth stays attached.
        if not self._capture_scene_depth():
            return 0
        inverses = decals[2] if len(decals) > 2 else box_inverse(models)
        # Image and blood decals first, in their order (blood over placed);
        # the wet ones after, drawn by the second draw.
        wet = params[keep, 2] > 2.5
        order = np.concatenate((keep[~wet], keep[wet]))
        wet_count = int(wet.sum())
        plain_count = count - wet_count
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
        gl.glUniform1i(u['sceneColor'], self._glass_scene_texture_unit)
        inverse_projection = glm.inverse(projection)
        gl.glUniformMatrix4fv(u['invProjection'], 1, gl.GL_FALSE,
                              glm.value_ptr(inverse_projection))
        gl.glUniform1i(u['reflectionsOn'],
                       1 if getattr(self, 'reflections_enabled', True) else 0)
        gl.glUniform1f(u['decalFadeEnd'], float(getattr(
            self, 'decal_reflection_fade', shaders.REFLECTION_FADE_END)))
        gl.glUniform1i(u['wetPass'], 0)
        gl.glUniform4f(u['decalRanges'], *self._decal_ranges)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
        gl.glUniform1i(u['decalTextures'], 0)

        self._decal_instances(count)
        staging = self._decal_staging[:count]
        staging[:, 0:16] = models[order]
        staging[:, 16:20] = params[order]
        staging[:, 20:36] = inverses[order]
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, staging.nbytes, staging)

        # The box's far faces, untested against depth: every pixel the box
        # covers is reached once, the camera inside the box or not. The
        # shader does the real test, against the copy.
        # Everything this pass changes is put back exactly as it was: the
        # passes after it (the transparent and trigger brushes, effects)
        # draw with the blend function, culling and texture unit they set
        # up before, decals or no decals.
        blend = bool(gl.glIsEnabled(gl.GL_BLEND))
        blend_func = tuple(int(gl.glGetIntegerv(name)) for name in (
            gl.GL_BLEND_SRC_RGB, gl.GL_BLEND_DST_RGB,
            gl.GL_BLEND_SRC_ALPHA, gl.GL_BLEND_DST_ALPHA))
        cull = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        cull_mode = int(gl.glGetIntegerv(gl.GL_CULL_FACE_MODE))
        depth_test = bool(gl.glIsEnabled(gl.GL_DEPTH_TEST))
        depth_write = bool(gl.glGetBooleanv(gl.GL_DEPTH_WRITEMASK))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_DST_COLOR, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_FRONT)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        try:
            if plain_count:
                self._decal_point(0)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, plain_count)
                self.render_stats.draw_calls += 1
            if wet_count:
                # Wet patches: over a copy of the frame so far (blood and
                # images included), drawn plainly over what is there.
                if self._capture_glass_scene() is not None:
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
                    gl.glBindVertexArray(self._decal_vao)
                    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
                    gl.glUseProgram(program)
                    gl.glUniform1i(u['wetPass'], 1)
                    gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                    self._decal_point(plain_count)
                    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, wet_count)
                    self.render_stats.draw_calls += 1
        finally:
            gl.glCullFace(cull_mode)
            if not cull:
                gl.glDisable(gl.GL_CULL_FACE)
            if depth_test:
                gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthMask(gl.GL_TRUE if depth_write else gl.GL_FALSE)
            gl.glBlendFuncSeparate(*blend_func)
            if not blend:
                gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
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
        self._decal_base = None
        self._decal_layers = 0
