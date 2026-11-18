"""Light cookies: an image a Light projects onto everything around it.

A Light with ``cookie_enabled`` true and a ``cookie_texture`` image projects
that image's actual RGB through 360 degrees, like a lamp shade with a picture
on it. The image is square and laid out with an *octahedral* direction map
(:data:`engine.shaders.LIGHT_COOKIE_GLSL`): every direction out of the light
is one texel, so a fragment's colour is read from the direction between the
light's centre and that fragment. The image therefore stretches with distance
in world space, exactly as a projected picture does, and the shader softens it
towards the light's radius by sampling a blurrier mip level (one sample,
hardware filtered -- never extra taps).

Orientation (Y is up)::

    image centre      -> straight down (the floor under the light)
    image right (+u)  -> world +X
    image top         -> world -Z
    image corners     -> straight up (the ceiling), folded outwards

so the floor under a light shows the picture the right way round in the
editor's top view, walls show the ring around the centre, and the ceiling
shows the folded corners.

The data path costs an ordinary light nothing:

* Every image is *interned* once, process-wide (:func:`intern_cookie`), to a
  small positive integer. A light's id is ``0`` unless the cookie is enabled
  and names an image -- the column ``EntityTable.light_cookie``.
* The renderer keeps one shared ``GL_TEXTURE_2D_ARRAY`` (:class:`CookieArray`)
  and writes ``layer + 1`` into the light UBO's unused ``indices.z``, or ``0``
  when the light has no cookie or its image cannot be read. The UBO does not
  grow, there is no extra pass, draw call or per-light binding, and the shader
  samples only for a light whose ``indices.z`` is positive.
"""

from __future__ import annotations

import ctypes
import io
import threading

import numpy as np

#: The id of "no cookie": an ordinary light.
NO_COOKIE = 0
#: Each layer's edge on the desktop renderer, and on low-power GPUs.
COOKIE_SIZE = 512
COOKIE_SIZE_LOW_POWER = 256
#: The most images one array holds. Lights past it light without a cookie.
MAX_COOKIE_LAYERS = 64
#: The image formats a cookie can be.
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.bmp', '.tga')

_lock = threading.Lock()
_ids: dict[str, int] = {}
_paths: list[str] = ['']           # index 0 is NO_COOKIE


def _normalise(path) -> str:
    if not isinstance(path, str):
        return ''
    return path.strip().replace('\\', '/')


def intern_cookie(path) -> int:
    """The process-wide id of the image at *path* (1, 2, ...); 0 for none.

    The same path always gets the same id, on every thread and every table,
    so a render snapshot and the table it was copied from agree.
    """
    path = _normalise(path)
    if not path:
        return NO_COOKIE
    with _lock:
        cid = _ids.get(path)
        if cid is None:
            cid = len(_paths)
            _ids[path] = cid
            _paths.append(path)
        return cid


def cookie_path(cookie_id) -> str:
    """The image path interned as *cookie_id*; '' for 0 or an unknown id."""
    cookie_id = int(cookie_id)
    with _lock:
        if 0 < cookie_id < len(_paths):
            return _paths[cookie_id]
    return ''


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def cookie_of(props) -> int:
    """A Light's cookie id from its properties: 0 unless ``cookie_enabled``
    is explicitly true and ``cookie_texture`` names an image."""
    if not isinstance(props, dict) or not _truthy(props.get('cookie_enabled', False)):
        return NO_COOKIE
    return intern_cookie(props.get('cookie_texture', ''))


def cookie_shadow_floor(props) -> float:
    """A cookie light's ``cookie_shadow_floor``, clamped to 0..1: how bright
    its image stays inside its own shadow (default
    :data:`engine.shaders.COOKIE_SHADOW_FLOOR`)."""
    from engine.shaders import COOKIE_SHADOW_FLOOR
    try:
        value = float(props.get('cookie_shadow_floor', COOKIE_SHADOW_FLOOR))
    except (TypeError, ValueError, AttributeError):
        value = COOKIE_SHADOW_FLOOR
    if value != value:              # NaN
        value = COOKIE_SHADOW_FLOOR
    return min(1.0, max(0.0, value))


def square_rgba(data, size):
    """*data* (encoded image bytes) as ``size*size*4`` RGBA bytes, or None.

    Cookies are square; a non-square image is scaled to square rather than
    refused, so a slightly-off export still projects. Rows run top first: the
    image's top is v = 0, which the shader maps to world -Z.
    """
    if not data:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert('RGBA')
            if image.size != (size, size):
                image = image.resize((size, size), Image.Resampling.LANCZOS)
            return image.tobytes()
    except Exception as exc:        # a broken file lights as an ordinary light
        print(f'[Cookie] cannot read image: {exc}')
        return None


def project_bytes(path):
    """The bytes of the desktop project's image at *path*, or None."""
    from engine.asset_archives import asset_bytes
    from engine.asset_paths import project_asset_path, warn_refused
    # A map's name for the image is data from outside: confined to the
    # project (POSIX and Windows rules alike, symlinks resolved) before
    # anything is opened.
    safe = project_asset_path(path)
    if safe is None:
        warn_refused(path, 'cookie image')
        return None
    try:
        return asset_bytes(safe)
    except OSError:
        return None


def _tex_image_3d(gl):
    """``(glTexImage3D, glTexSubImage3D)`` for *gl*: PyOpenGL's raw entry
    points when *gl* is a PyOpenGL module (its wrappers probe desktop-only
    pixel store state, which GLES rejects -- player/render/assets.upload_rgba),
    otherwise whatever *gl* itself provides (another binding, a recorder)."""
    if str(getattr(gl, '__name__', '')).startswith('OpenGL'):
        from OpenGL.raw.GL.VERSION.GL_1_2 import glTexImage3D, glTexSubImage3D
        return glTexImage3D, glTexSubImage3D
    return gl.glTexImage3D, gl.glTexSubImage3D


def bind_placeholder(gl, unit, cache):
    """Bind a complete 1x1 RGBA ``GL_TEXTURE_2D_ARRAY`` to *unit*, made once
    and kept in *cache* (a dict the caller owns, one per GL context).

    Every lit program declares the cookie sampler. While no light has a
    cookie nothing samples it, but a driver may still check each active
    sampler at draw time: macOS logs "unit N GLD_TEXTURE_INDEX_2D_ARRAY is
    unloadable" and substitutes zeros when the unit holds nothing usable.
    The shadow samplers keep a placeholder cube map for the same reason.
    """
    texture = cache.get('cookie_placeholder')
    if texture is None:
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
        glTexImage3D, _sub = _tex_image_3d(gl)
        pixel = ctypes.create_string_buffer(b'\xff\xff\xff\xff', 4)
        glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_RGBA8, 1, 1, 1, 0,
                     gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, ctypes.cast(pixel, ctypes.c_void_p))
        # One level and no mipmap filter: complete as it is.
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        cache['cookie_placeholder'] = texture
    gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
    gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
    gl.glActiveTexture(gl.GL_TEXTURE0)
    return texture


def release_placeholder(gl, cache):
    """Delete the placeholder :func:`bind_placeholder` made in *cache*, if
    any: its owner is being cleaned up."""
    texture = cache.pop('cookie_placeholder', None)
    if texture is not None:
        try:
            gl.glDeleteTextures([texture])
        except Exception:
            pass


class CookieArray:
    """The one ``GL_TEXTURE_2D_ARRAY`` that holds every cookie image.

    *gl* is the GL module in use (PyOpenGL on the desktop, the player's own on
    GLES); *read(path)* returns an image's bytes or None (the desktop
    project's assets when not given). Layers are given out
    on first use and kept; the array is reallocated only when a new image
    needs more room than it has (doubling), and mipmaps are rebuilt then.
    """

    def __init__(self, gl, read=None, size=COOKIE_SIZE):
        self.gl = gl
        self.read = read
        self.size = int(size)
        self.texture = None
        self.capacity = 0
        #: cookie id -> array layer, or -1 when the image cannot be read.
        self._layer = {}
        self._pixels = []           # per layer, kept for reallocation
        #: Bumped whenever a layer's meaning changes (the UBO cache key).
        self.version = 0
        #: Lookup table: cookie id -> layer + 1 (0 = sample nothing).
        self._lut = np.zeros(1, dtype=np.int32)

    def _load(self, cookie_id):
        if len(self._pixels) >= MAX_COOKIE_LAYERS:
            return -1
        path = cookie_path(cookie_id)
        if not path.lower().endswith(IMAGE_SUFFIXES):
            return -1
        read = self.read or project_bytes
        pixels = square_rgba(read(path), self.size)
        if pixels is None:
            return -1
        self._pixels.append(pixels)
        return len(self._pixels) - 1

    def layers(self, cookie_ids):
        """``layer + 1`` for each id in *cookie_ids*, 0 where there is none.

        Loads images the array has not seen yet. Vectorised: an ordinary
        frame is one table lookup.
        """
        ids = np.asarray(cookie_ids, dtype=np.int32)
        if not len(ids) or not ids.any():
            return np.zeros(len(ids), dtype=np.int32)
        top = int(ids.max())
        if top >= len(self._lut) or any(
                int(c) not in self._layer for c in np.unique(ids[ids > 0])):
            self._admit(np.unique(ids[ids > 0]))
        ids = np.clip(ids, 0, len(self._lut) - 1)
        return self._lut[ids]

    def _admit(self, cookie_ids):
        before = len(self._pixels)
        for cid in cookie_ids:
            cid = int(cid)
            if cid not in self._layer:
                self._layer[cid] = self._load(cid)
        lut = np.zeros(max(self._layer) + 1, dtype=np.int32)
        for cid, layer in self._layer.items():
            lut[cid] = layer + 1 if layer >= 0 else 0
        self._lut = lut
        if len(self._pixels) != before:
            self._upload(before)
            self.version += 1

    def _upload(self, first=0):
        """Write layers *first*.. into the GL array (all of them when it has
        to grow), then rebuild its mipmaps."""
        gl = self.gl
        count = len(self._pixels)
        if not count:
            return
        if self.texture is None:
            self.texture = int(gl.glGenTextures(1))
        capacity = max(4, self.capacity)
        while capacity < count:
            capacity *= 2
        size = self.size
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        # The raw entry points: PyOpenGL's wrappers probe desktop-only pixel
        # store state, which GLES rejects (player/render/assets.upload_rgba).
        glTexImage3D, glTexSubImage3D = _tex_image_3d(gl)
        if capacity != self.capacity:
            glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_RGBA8, size, size,
                         capacity, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            self.capacity = capacity
            first = 0               # a new store: every layer again
        for index in range(first, count):
            data = ctypes.create_string_buffer(self._pixels[index], len(self._pixels[index]))
            glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, 0, 0, 0, index, size, size, 1,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                            ctypes.cast(data, ctypes.c_void_p))
        # Hardware mipmaps carry the softening: the shader asks for a
        # blurrier level with distance, the sampler filters between levels.
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D_ARRAY)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)

    def bind(self, unit):
        """Bind the array to texture *unit*; nothing to bind is a no-op."""
        if self.texture is None:
            return False
        gl = self.gl
        gl.glActiveTexture(gl.GL_TEXTURE0 + unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self.texture)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return True

    def close(self):
        if self.texture is not None:
            try:
                self.gl.glDeleteTextures([self.texture])
            except Exception:
                pass
        self.texture = None
        self.capacity = 0
        self._layer.clear()
        self._pixels.clear()
        self._lut = np.zeros(1, dtype=np.int32)
        self.version += 1
