"""Sprite textures as layers of one texture array.

The billboard pass is blended with depth writes off, so the order it draws in
is part of what it draws: a far sprite drawn after a near one lands on top of
it.  Back-to-front is the only correct order, and it interleaves textures --
two monster types walking among each other alternate texture at almost every
step.  With one GL texture per sprite image the pass therefore had two bad
choices: draw in depth order and pay roughly one draw per sprite, or group by
texture and draw different sprites in the wrong order where they overlap.

Moving the texture out of the draw state removes the choice.  Every sprite
image becomes a layer of one ``GL_TEXTURE_2D_ARRAY`` and the layer index is
instance data like the position, so the whole pass is one instanced draw in
exact depth order.  In render-key terms: the texture no longer "cannot vary
within one draw", so it leaves the key, and the key of the sprite pass is empty.

What goes into a layer is the sprite's existing 2D texture, found exactly as
before (:meth:`RendererCore._sprite_gl_ids` resolves recipes to GL ids, with
every override and fallback it had).  Layers are keyed by that GL id, so there
is no second loader and no second naming scheme.

Filling a layer
---------------
Pixels are read back from the source texture once, resampled on the CPU and
uploaded with ``glTexSubImage3D`` -- every mip level included.  The array is
never a render target: no framebuffer blit, no ``glGenerateMipmap``.  That is a
measured choice, not a stylistic one.  Filled by blit, the array was a texture
the driver had rendered into, and Mesa's llvmpipe then made the draw that
sampled it wait for the whole frame's pending rendering -- 5 ms per frame in
the live benchmark, against 0.14 ms once that wait was paid elsewhere.  Uploads
carry no such hazard on any driver, and the cost moves to the one moment a
sprite image is first seen.

Layers are square and all the same size, the smallest power of two that holds
the largest imported sprite (clamped to ``[min_size, max_size]``).  The
billboard quad maps texture coordinates 0..1 onto its world-sized rectangle
whatever the image's aspect ratio, so stretching an image to fill a square
layer changes nothing about where it lands -- only how it is resampled.
Wrapping stays ``GL_REPEAT`` and filtering trilinear, as the 2D sprite textures
had; the mip chain is box-filtered, as ``glGenerateMipmap`` would build it.

GL-dependent by nature; everything is created lazily on the thread that owns
the context, the way the renderer's other GL objects are.
"""

from __future__ import annotations

import numpy as np
import OpenGL.GL as gl
from PIL import Image


def _next_pow2(value: int) -> int:
    value = max(1, int(value))
    return 1 << (value - 1).bit_length()


class SpriteLayers:
    """GL texture id -> layer of one ``GL_TEXTURE_2D_ARRAY``."""

    def __init__(self, max_size=512, min_size=16, initial_capacity=8):
        self.max_size = int(max_size)
        self.min_size = int(min_size)
        self.initial_capacity = int(initial_capacity)
        #: The array texture, 0 until the first import.
        self.texture = 0
        #: Edge length of every layer.
        self.size = 0
        #: Layers allocated, and layers in use.
        self.capacity = 0
        self.count = 0
        #: layer -> the GL 2D texture it was filled from, so a resize can
        #: resample the original rather than an already-resampled layer.
        self._sources: list = []
        #: Dense ``GL id -> layer`` lookup (-1 = not imported). GL texture
        #: names are small integers, so a gather is the whole per-frame cost.
        self._layer_by_gl = np.full(64, -1, dtype=np.int32)
        self._max_layers = None
        #: Set once an import cannot complete (more sprite images than the
        #: driver allows layers, a source that cannot be read back).  From then
        #: on :meth:`layers_for` answers ``None`` and the caller keeps its
        #: per-texture path: a half-filled array would draw blank sprites.
        self.disabled = False

    # -- per-frame ---------------------------------------------------------

    def layers_for(self, gl_ids):
        """Layer index per GL texture id in *gl_ids*, importing unseen ones.

        Returns an ``int32`` array aligned with *gl_ids*, or ``None`` when an
        image could not be placed in the array.
        """
        if self.disabled:
            return None
        gl_ids = np.asarray(gl_ids, dtype=np.int64)
        if not len(gl_ids):
            return np.empty(0, dtype=np.int32)
        top = int(gl_ids.max())
        if top >= len(self._layer_by_gl):
            grown = np.full(max(top + 1, len(self._layer_by_gl) * 2), -1,
                            dtype=np.int32)
            grown[:len(self._layer_by_gl)] = self._layer_by_gl
            self._layer_by_gl = grown
        layers = self._layer_by_gl[gl_ids]
        missing = layers < 0
        if missing.any():
            if not self._import(np.unique(gl_ids[missing])):
                self.reset()
                self.disabled = True
                return None
            layers = self._layer_by_gl[gl_ids]
        return layers

    # -- import ------------------------------------------------------------

    def _import(self, new_ids):
        """Give each id in *new_ids* a layer, resizing the array if needed."""
        images = {}
        needed = self.size
        for tid in new_ids:
            image = self._read_texture(int(tid))
            if image is None:
                return False
            images[int(tid)] = image
            needed = max(needed, _next_pow2(max(image.size)))
        needed = min(max(needed, self.min_size), self.max_size)
        total = self.count + len(new_ids)
        limit = self._layer_limit()
        if total > limit:
            return False

        refill = []
        if needed != self.size or total > self.capacity:
            capacity = max(self.capacity, self.initial_capacity)
            while capacity < total:
                capacity *= 2
            self._allocate(needed, min(capacity, limit))
            refill = list(enumerate(self._sources))

        start = self.count
        for offset, tid in enumerate(new_ids):
            self._sources.append(int(tid))
            self._layer_by_gl[int(tid)] = start + offset
        self.count = total

        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        try:
            for layer, tid in refill:
                image = self._read_texture(tid)
                if image is None:
                    return False
                self._upload(layer, image)
            for offset, tid in enumerate(new_ids):
                self._upload(start + offset, images[int(tid)])
        finally:
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            # Back to the GL default. The context is shared with Qt's text
            # rendering, whose glyph uploads assume 4-byte rows: left at 1,
            # every glyph not a multiple of 4 wide came out sheared.
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        return True

    def _layer_limit(self):
        if self._max_layers is None:
            try:
                self._max_layers = int(
                    gl.glGetIntegerv(gl.GL_MAX_ARRAY_TEXTURE_LAYERS))
            except Exception:
                self._max_layers = 256      # the GL 3.3 minimum
        return self._max_layers

    @staticmethod
    def _read_texture(tex_id):
        """Level 0 of a 2D texture as an RGBA ``PIL.Image``, or ``None``."""
        try:
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            w = int(gl.glGetTexLevelParameteriv(
                gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH))
            h = int(gl.glGetTexLevelParameteriv(
                gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT))
            if w <= 0 or h <= 0:
                return None
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
            raw = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
                                   gl.GL_UNSIGNED_BYTE, outputType=bytes)
        except Exception:
            return None
        finally:
            gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 4)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        if raw is None or len(raw) < w * h * 4:
            return None
        return Image.frombytes('RGBA', (w, h), bytes(raw[:w * h * 4]))

    def _levels(self):
        return self.size.bit_length()          # size is a power of two

    def _allocate(self, size, capacity):
        """(Re)create the array at *size* x *size* x *capacity*, all levels."""
        old = self.texture
        tex = int(gl.glGenTextures(1))
        self.texture = tex
        self.size = int(size)
        self.capacity = int(capacity)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, tex)
        edge = self.size
        for level in range(self._levels()):
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, level, gl.GL_RGBA8,
                            edge, edge, capacity, 0, gl.GL_RGBA,
                            gl.GL_UNSIGNED_BYTE, None)
            edge = max(1, edge // 2)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_BASE_LEVEL, 0)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MAX_LEVEL,
                           self._levels() - 1)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MIN_FILTER,
                           gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        if old:
            gl.glDeleteTextures([old])

    def _upload(self, layer, image):
        """Resample *image* into *layer* and upload its whole mip chain."""
        edge = self.size
        if image.size != (edge, edge):
            enlarging = max(image.size) <= edge
            image = image.resize(
                (edge, edge),
                Image.BILINEAR if enlarging else Image.LANCZOS)
        for level in range(self._levels()):
            if level:
                edge = max(1, edge // 2)
                image = image.resize((edge, edge), Image.BOX)
            gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, level, 0, 0, layer,
                               edge, edge, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                               image.tobytes())

    # -- lifetime ------------------------------------------------------------

    def reset(self):
        """Forget every layer (the textures they came from may be gone)."""
        self.cleanup()

    def cleanup(self):
        try:
            if self.texture:
                gl.glDeleteTextures([self.texture])
        except Exception:
            pass                   # teardown without a context is not an error
        self.texture = 0
        self.size = self.capacity = self.count = 0
        self._sources = []
        self._layer_by_gl.fill(-1)
