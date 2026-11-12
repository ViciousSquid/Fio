"""Player decal pass: the desktop's projected decals, on OpenGL ES 3.

The logic thread publishes the same ``decals`` the desktop renderer draws --
box matrices, ``(variant, opacity, kind, flags)`` and the boxes' inverses
from ``engine.decal_table.DecalTable`` -- blood and the level's placed image
and wet decals alike. This pass draws them with the shared ``decal.vert`` /
``decal.frag``: each box is an instance of the unit cube, and the fragment
shader puts the surface under each pixel (read from a copy of the depth
buffer) in the box's space and paints the decal's image where it lies inside.

What differs from the desktop is only what a phone can afford, as with the
player's water: wet decals darken the surface but reflect nothing (the
screen-space reflection march is compiled out), and the depth the boxes are
tested against is a blit of the world target's depth buffer -- the renderer
draws into its offscreen target on any frame with a decal to show, since a
window's own depth buffer cannot be copied portably.

The images are one texture array in ``engine.decal_textures`` order, read
from whatever source the world gave that module (the played package, and
the blood images bundled with the player), so a placed decal's layer and
blood's ranges are the logic side's.
"""
import ctypes
import io

import numpy as np

from engine import decal_textures
from engine.decal_table import box_inverse
from engine.shaders import DEFAULT_SHADERS
from player.gles_shaders import translate, for_desktop
from . import glmath
from .gles_context import compile_program
from .uniforms import location

#: Side of a layer of the decal texture array; images are scaled to it. Half
#: the desktop's: a decal a few hundred units across rarely covers more.
DECAL_TEXTURE_SIZE = 128
#: Floats an instance: a 4x4 box matrix, params and the box's inverse.
_INSTANCE_FLOATS = 36
_INSTANCE_VEC4S = _INSTANCE_FLOATS // 4
#: A box covering less than this many pixels across (its radius) is not drawn.
MIN_PIXEL_RADIUS = 0.75

_CUBE = np.array([
    # The desktop renderer's unit cube (engine/renderer/core/geometry.py),
    # positions only: the pass culls its front faces, as the desktop does.
    -.5, -.5, -.5, .5, -.5, -.5, .5, .5, -.5, .5, .5, -.5, -.5, .5, -.5, -.5, -.5, -.5,
    -.5, -.5, .5, .5, .5, .5, .5, -.5, .5, .5, .5, .5, -.5, -.5, .5, -.5, .5, .5,
    -.5, .5, .5, -.5, -.5, -.5, -.5, .5, -.5, -.5, -.5, -.5, -.5, .5, .5, -.5, -.5, .5,
    .5, .5, .5, .5, .5, -.5, .5, -.5, -.5, .5, -.5, -.5, .5, -.5, .5, .5, .5, .5,
    -.5, -.5, -.5, .5, -.5, .5, .5, -.5, -.5, .5, -.5, .5, -.5, -.5, -.5, -.5, -.5, .5,
    -.5, .5, -.5, .5, .5, -.5, .5, .5, .5, .5, .5, .5, -.5, .5, .5, -.5, .5, -.5,
], dtype=np.float32)


def visible_decals(models, clip, eye, viewport_height, focal):
    """Indices of the boxes in *models* (n, 16, column-major) whose bounding
    spheres touch the frustum of *clip* (projection @ view, row-major numpy)
    and are at least :data:`MIN_PIXEL_RADIUS` across on screen."""
    models = np.asarray(models, dtype=np.float32)
    if not len(models):
        return np.zeros(0, dtype=np.intp)
    centres = models[:, 12:15].astype(np.float64)
    radius = 0.5 * np.sqrt((models[:, 0:3] ** 2).sum(1) + (models[:, 4:7] ** 2).sum(1)
                           + (models[:, 8:11] ** 2).sum(1))
    m = np.asarray(clip, dtype=np.float64)
    planes = np.stack((m[3] + m[0], m[3] - m[0], m[3] + m[1],
                       m[3] - m[1], m[3] + m[2], m[3] - m[2]))
    planes /= np.maximum(np.linalg.norm(planes[:, :3], axis=1), 1e-12)[:, None]
    inside = (centres @ planes[:, :3].T + planes[:, 3] >= -radius[:, None]).all(1)
    distance = np.linalg.norm(centres - np.asarray(eye, dtype=np.float64)[:3], axis=1)
    pixels = radius * abs(float(focal)) * 0.5 * float(viewport_height)
    big_enough = (distance <= radius) | (pixels >= MIN_PIXEL_RADIUS * distance)
    return np.flatnonzero(inside & big_enough)


def has_decals(decals):
    """Whether a published ``decals`` value holds any box."""
    return decals is not None and len(decals) >= 2 and len(decals[0]) > 0


class PlayerDecals:
    """The decal program, its instance buffer, texture array and depth copy."""

    def __init__(self, gl, gles=True):
        self.gl, self.gles = gl, gles
        self.program = None
        self.vao = self.cube = self.instances = None
        self.capacity = 0
        self.textures = None
        self.layers = 0
        self.images_version = None
        self.ranges = (0.0, 1.0, 0.0, 1.0)
        self.depth_fbo = self.depth_texture = None
        self.color_texture = None
        self.copy_size = None
        self.uniforms = {}
        self.failed = False
        #: Kept between frames: the per-frame instance rows are written here.
        self.staging = np.empty((0, _INSTANCE_FLOATS), dtype=np.float32)
        #: Boxes drawn by the last frame, and those culled (tests, stats).
        self.drawn = 0
        self.culled = 0

    # -- GL objects ------------------------------------------------------
    def _prepare(self):
        gl = self.gl
        convert = translate if self.gles else (lambda source, stage: for_desktop(source))
        fragment = DEFAULT_SHADERS['decal.frag']
        # No reflection march on a phone: the wet pass darkens only.
        fragment = fragment.replace('uniform int reflectionsOn;', 'const int reflectionsOn = 0;')
        self.program = compile_program(convert(DEFAULT_SHADERS['decal.vert'], 'vertex'),
                                       convert(fragment, 'fragment'))
        self.uniforms = {name: location(gl, self.program, name) for name in (
            'projection', 'view', 'sceneDepth', 'decalTextures', 'decalRanges',
            'invViewProjection', 'viewport', 'viewPos', 'wetPass', 'sceneColor',
            'invProjection', 'decalFadeEnd')}
        self.vao = gl.glGenVertexArrays(1)
        self.cube, self.instances = gl.glGenBuffers(2)
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.cube)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, _CUBE.nbytes, _CUBE, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.instances)
        self.capacity = 0
        self._point(0)
        for i in range(_INSTANCE_VEC4S):
            gl.glEnableVertexAttribArray(3 + i)
            gl.glVertexAttribDivisor(3 + i, 1)
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

    def _point(self, base):
        """The instance attributes from instance *base* on (the vertex array
        and the instance buffer bound)."""
        gl = self.gl
        stride = _INSTANCE_FLOATS * 4
        for i in range(_INSTANCE_VEC4S):
            gl.glVertexAttribPointer(3 + i, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                     ctypes.c_void_p(base * stride + i * 16))

    def _texture_array(self):
        """Every decal image as one texture array; rebuilt when the list of
        images changes. 0 when there are none."""
        version, names = decal_textures.images()
        if version == self.images_version:
            return self.textures or 0
        from PIL import Image
        gl = self.gl
        if self.textures:
            gl.glDeleteTextures([self.textures])
            self.textures = None
        self.images_version = version
        self.ranges = decal_textures.blood_ranges(names)
        size = DECAL_TEXTURE_SIZE
        blank = np.zeros((size, size, 4), dtype=np.uint8)
        layers = []
        for name in names:
            raw = decal_textures.read_image(name)
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    layers.append(np.asarray(image.convert('RGBA').resize(
                        (size, size), Image.LANCZOS), dtype=np.uint8))
            except Exception as exc:
                # A blank layer keeps every later image on its layer.
                print(f'[Decals] image {name} not loaded: {exc}', flush=True)
                layers.append(blank)
        self.layers = len(layers)
        if not layers:
            return 0
        data = np.ascontiguousarray(np.stack(layers))
        self.textures = gl.glGenTextures(1)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.textures)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_RGBA8, size, size, len(layers), 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D_ARRAY)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        return self.textures

    def _copies(self, width, height):
        """The depth (and colour, for wet decals) copy textures, sized."""
        from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D
        gl = self.gl
        if self.depth_fbo is None:
            self.depth_fbo = gl.glGenFramebuffers(1)
            self.depth_texture, self.color_texture = gl.glGenTextures(2)
        if self.copy_size == (width, height):
            return
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.depth_texture)
        # The world target's own format: a depth blit needs them to match.
        glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH24_STENCIL8, width, height, 0,
                     gl.GL_DEPTH_STENCIL, gl.GL_UNSIGNED_INT_24_8, None)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.color_texture)
        glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                     gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        previous = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.depth_fbo)
        gl.glFramebufferTexture2D(gl.GL_DRAW_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                  gl.GL_TEXTURE_2D, self.depth_texture, 0)
        complete = gl.glCheckFramebufferStatus(gl.GL_DRAW_FRAMEBUFFER) == gl.GL_FRAMEBUFFER_COMPLETE
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, previous)
        if not complete:
            raise RuntimeError('decal depth copy framebuffer is incomplete')
        self.copy_size = (width, height)

    def _copy_depth(self, width, height):
        """Blit the current world target's depth into the depth texture."""
        gl = self.gl
        self._copies(width, height)
        draw = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, draw)
        gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self.depth_fbo)
        try:
            gl.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height,
                                 gl.GL_DEPTH_BUFFER_BIT, gl.GL_NEAREST)
        finally:
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, draw)
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, read)

    def _copy_color(self, width, height):
        """Copy the frame so far, for the wet pass to darken."""
        gl = self.gl
        read = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER,
                             int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING)))
        gl.glActiveTexture(gl.GL_TEXTURE2)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.color_texture)
        try:
            gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, 0, 0, width, height)
        finally:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, read)

    # -- the pass --------------------------------------------------------
    def render(self, view, projection, eye, decals, width, height):
        """Paint *decals* onto the frame drawn so far, in the world target
        bound for drawing. Returns the boxes drawn."""
        self.drawn = self.culled = 0
        if self.failed or not has_decals(decals):
            return 0
        models, params = np.asarray(decals[0], np.float32), np.asarray(decals[1], np.float32)
        clip = np.asarray(projection, dtype=np.float64) @ np.asarray(view, dtype=np.float64)
        keep = visible_decals(models, clip, eye, height, np.asarray(projection)[1, 1])
        self.culled = len(models) - len(keep)
        if not len(keep):
            return 0
        gl = self.gl
        if self.program is None:
            try:
                self._prepare()
            except Exception as exc:
                # A device whose driver refuses the pass plays without decals.
                print(f'[Decals] disabled: {exc}', flush=True)
                self.failed = True
                return 0
        if not self._texture_array():
            return 0
        inverses = (np.asarray(decals[2], np.float32) if len(decals) > 2
                    else box_inverse(models))
        wet = params[keep, 2] > 2.5
        order = np.concatenate((keep[~wet], keep[wet]))
        wet_count = int(wet.sum())
        plain_count = len(order) - wet_count
        if len(self.staging) < len(order):
            self.staging = np.empty((max(64, len(order), 2 * len(self.staging)),
                                     _INSTANCE_FLOATS), dtype=np.float32)
        instances = self.staging[:len(order)]
        instances[:, 0:16] = models[order]
        instances[:, 16:20] = params[order]
        instances[:, 20:36] = inverses[order]

        self._copy_depth(width, height)
        if wet_count:
            self._copy_color(width, height)
        u, prog = self.uniforms, self.program
        gl.glUseProgram(prog)
        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glmath.column_major(projection))
        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glmath.column_major(view))
        gl.glUniformMatrix4fv(u['invViewProjection'], 1, gl.GL_FALSE,
                              glmath.column_major(np.linalg.inv(clip)))
        gl.glUniformMatrix4fv(u['invProjection'], 1, gl.GL_FALSE,
                              glmath.column_major(np.linalg.inv(np.asarray(projection, np.float64))))
        gl.glUniform4f(u['viewport'], 0.0, 0.0, float(max(width, 1)), float(max(height, 1)))
        gl.glUniform3f(u['viewPos'], float(eye[0]), float(eye[1]), float(eye[2]))
        gl.glUniform4f(u['decalRanges'], *self.ranges)
        gl.glUniform1f(u['decalFadeEnd'], 0.0)
        gl.glUniform1i(u['wetPass'], 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.textures)
        gl.glUniform1i(u['decalTextures'], 0)
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.depth_texture)
        gl.glUniform1i(u['sceneDepth'], 1)
        gl.glActiveTexture(gl.GL_TEXTURE2)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.color_texture)
        gl.glUniform1i(u['sceneColor'], 2)

        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.instances)
        if len(order) > self.capacity:
            self.capacity = max(64, len(order), self.capacity * 2)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, self.capacity * _INSTANCE_FLOATS * 4,
                            None, gl.GL_DYNAMIC_DRAW)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, instances.nbytes, instances)
        # The box's far faces, untested against depth: every covered pixel is
        # reached once, with the camera inside the box or not. The shader
        # tests against the copy.
        gl.glEnable(gl.GL_BLEND)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glFrontFace(gl.GL_CCW)
        gl.glCullFace(gl.GL_FRONT)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(False)
        try:
            if plain_count:
                # Stained, not painted: dst * (src.rgb + 1 - src.a).
                gl.glBlendFunc(gl.GL_DST_COLOR, gl.GL_ONE_MINUS_SRC_ALPHA)
                self._point(0)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, plain_count)
            if wet_count:
                gl.glUniform1i(u['wetPass'], 1)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                self._point(plain_count)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, wet_count)
        finally:
            self._point(0)
            gl.glCullFace(gl.GL_BACK)
            gl.glDisable(gl.GL_CULL_FACE)
            gl.glDisable(gl.GL_BLEND)
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthMask(True)
            gl.glBindVertexArray(0)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
            for unit, target in ((2, gl.GL_TEXTURE_2D), (1, gl.GL_TEXTURE_2D),
                                 (0, gl.GL_TEXTURE_2D_ARRAY)):
                gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
                gl.glBindTexture(target, 0)
        self.drawn = len(order)
        return self.drawn

    def forget(self):
        """The context was lost: its objects went with it."""
        self.program = self.vao = self.cube = self.instances = None
        self.textures = self.depth_fbo = self.depth_texture = self.color_texture = None
        self.capacity = self.layers = 0
        self.images_version = self.copy_size = None
        self.failed = False

    def close(self):
        gl = self.gl
        if self.vao:
            gl.glDeleteVertexArrays(1, [self.vao])
            gl.glDeleteBuffers(2, [self.cube, self.instances])
        if self.program:
            gl.glDeleteProgram(self.program)
        if self.textures:
            gl.glDeleteTextures([self.textures])
        if self.depth_fbo:
            gl.glDeleteFramebuffers(1, [self.depth_fbo])
            gl.glDeleteTextures([self.depth_texture, self.color_texture])
        self.forget()
