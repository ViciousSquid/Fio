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
from engine import decal_text, decal_textures
from engine.decal_table import IMAGE, box_inverse
from engine.asset_archives import asset_source

DECAL_FOLDER = os.path.join('assets', 'textures', 'decals')
#: Side of a layer of the decal texture array; images are scaled to it.
DECAL_TEXTURE_SIZE = 256
#: Largest side of a layer of the placed image decals' own texture array.
#: A placed image -- a sign, a poster, a 512x64 banner -- is painted from its
#: image at the image's own resolution (up to this), each of its axes kept,
#: not squeezed into the shared array's 256x256 layers (which stay for blood
#: and wet masks). The decal's box, sized in the editor, sets its shape.
IMAGE_TEXTURE_MAX = 1024
#: The emissive decals' cap (the same array; the name they were given first).
EMISSIVE_TEXTURE_MAX = IMAGE_TEXTURE_MAX
#: Most bytes the placed images' array may take (with its mipmaps): past it
#: its side is halved, so a map of many large images still fits on a small
#: GPU.
IMAGE_TEXTURE_BUDGET = 64 * 1024 * 1024
#: Emissive decals a frame from which :func:`glow_layers` and
#: :func:`remap_glow_rows` use NumPy rather than a Python loop. A small-N
#: crossover (the invariant in tests/performance/test_small_n_invariant.py):
#: below it the loop is cheaper -- NumPy's fixed cost a call is paid however
#: few the rows -- and a map has a handful of emissive decals. Measured
#: (CPython 3.13, NumPy 2; re-measure with ``pytest
#: tests/performance/test_small_n_invariant.py -k benchmark -s
#: --run-benchmarks``)::
#:
#:     decals    loop     NumPy
#:          1    0.9 us   10.0 us
#:         16    3.3 us   12.5 us
#:         64   10.1 us   14.7 us
#:         96   14.3 us   15.8 us
#:        128   18.9 us   17.2 us
#:       1024  147.8 us   56.5 us
GLOW_REMAP_VECTOR_FROM = 110
#: Layers of the placed images' array that hold their image unstretched
#: (decal.frag's imageLayerScale); any past these are stretched to the
#: layer, as the shared array's are.
IMAGE_LAYER_SCALES = 64
#: An empty shared-layer lookup (no emissive layers loaded).
_NO_LAYERS = np.empty(0, dtype=np.int64)
#: Floats an instance: a 4x4 box matrix, (variant, opacity, kind, flags)
#: and the box's inverse.
_INSTANCE_FLOATS = 36
#: vec4 attributes an instance: four model columns, params, four inverse
#: columns -- locations 3..11.
_INSTANCE_VEC4S = _INSTANCE_FLOATS // 4
#: A decal covering less than this many pixels across (its radius, on
#: screen) is not drawn.
MIN_PIXEL_RADIUS = 0.5


def _mip_chain(layer):
    """*layer* (side x side RGBA bytes, side a power of two) and each mipmap
    level below it, halved by averaging -- as glGenerateMipmap makes them --
    for updating one layer of the array in place."""
    level = np.ascontiguousarray(layer, dtype=np.uint8)
    yield level
    wide = level.astype(np.uint16)
    while wide.shape[0] > 1:
        wide = (wide[0::2, 0::2] + wide[1::2, 0::2] + wide[0::2, 1::2]
                + wide[1::2, 1::2] + 2) // 4
        yield np.ascontiguousarray(wide, dtype=np.uint8)


def emissive_side(sizes):
    """The side of the placed images' texture array for images of *sizes*
    (``(width, height)`` each): the power of two that holds the largest of
    their sides, from :data:`DECAL_TEXTURE_SIZE` to :data:`IMAGE_TEXTURE_MAX`
    -- so neither axis of any of them loses detail (a 512x64 banner keeps its
    512 across and its 64 up)."""
    largest = max((max(int(w), int(h)) for w, h in sizes), default=0)
    side = DECAL_TEXTURE_SIZE
    while side < largest and side < IMAGE_TEXTURE_MAX:
        side *= 2
    return side


#: The same, by what it is for now.
image_side = emissive_side


def _unstretched_layer(image, side):
    """``(side x side RGBA, (u, v) scale)``: PIL *image* fitted into a
    square layer with its aspect kept -- its longer side the layer's -- from
    the corner, the rest its edge pixels repeated (so a blurred mipmap does
    not fade its edges); the scale is the part of the layer it fills."""
    from PIL import Image
    width, height = image.size
    fit = side / float(max(width, height, 1))
    w = max(1, min(side, int(round(width * fit))))
    h = max(1, min(side, int(round(height * fit))))
    fitted = np.asarray(image.convert('RGBA').resize((w, h), Image.LANCZOS), dtype=np.uint8)
    layer = np.pad(fitted, ((0, side - h), (0, side - w), (0, 0)), mode='edge')
    return np.ascontiguousarray(layer), (w / float(side), h / float(side))


def budget_side(side, layers):
    """*side* halved until *layers* layers of it (and their mipmaps) fit
    :data:`IMAGE_TEXTURE_BUDGET`, never below :data:`DECAL_TEXTURE_SIZE`."""
    while side > DECAL_TEXTURE_SIZE and layers * side * side * 16 // 3 > IMAGE_TEXTURE_BUDGET:
        side //= 2
    return side


def glow_layers(shared):
    """The distinct shared layers (sorted) the emissive decals' rows name;
    *shared* is their layer column as int64."""
    if len(shared) < GLOW_REMAP_VECTOR_FROM:
        return sorted(set(shared.tolist()))
    return np.unique(shared).tolist()


def remap_glow_rows(rows, shared, index, lut):
    """Point the emissive decals' instance *rows* at their layers in the
    full-resolution array: column 16 from shared layer (*shared*, int64) to
    its own layer (*index*: shared -> own; *lut*: the same as an array, -1
    where there is none). A layer not in it (an unreadable image) draws
    nothing rather than another image: its opacity (column 17) goes to 0.
    A Python loop below :data:`GLOW_REMAP_VECTOR_FROM` rows, NumPy from it;
    the same rows either way."""
    if len(shared) < GLOW_REMAP_VECTOR_FROM:
        _remap_glow_rows_loop(rows, shared.tolist(), index)
    else:
        _remap_glow_rows_table(rows, shared, lut)


def _remap_glow_rows_loop(rows, layers, index):
    for row, layer in enumerate(layers):
        own = index.get(layer)
        if own is not None:
            rows[row, 16] = float(own)
        else:
            rows[row, 17] = 0.0


def _remap_glow_rows_table(rows, shared, lut):
    inside = (shared >= 0) & (shared < len(lut))
    mapped = np.full(shared.shape, -1, dtype=np.int64)
    mapped[inside] = lut[shared[inside]]
    found = mapped >= 0
    rows[found, 16] = mapped[found]
    rows[~found, 17] = 0.0


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



def _decode_layers(names, size):
    """:func:`_decal_layer` for each decal file in *names*, in order, on a
    few worker threads; None for one that cannot be read (it is reported, and
    kept as a blank layer so every later image keeps its layer)."""
    def load(name):
        try:
            return _decal_layer(asset_source(os.path.join(DECAL_FOLDER, name)), size)
        except Exception as exc:
            print(f'[Renderer] decal texture {name} not loaded: {exc}')
            return None

    if len(names) < 4:
        return [load(name) for name in names]
    from concurrent.futures import ThreadPoolExecutor
    workers = max(1, min(4, (os.cpu_count() or 1)))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix='DecalDecode') as pool:
        return list(pool.map(load, names))


def _decal_layer(source, size):
    """The decal image at *source* as a ``size`` x ``size`` RGBA layer.

    Every library image is decoded for the first frame that shows decals, and
    they are up to 1536 px for a 256 px layer. A JPEG is decoded at a reduced
    scale (``draft``) and the picture box-reduced to within twice the layer
    size before the Lanczos pass (``reducing_gap``): Pillow's own fast path,
    about a third of the cost, and visually the same at 256 px.
    """
    from PIL import Image
    with Image.open(source) as image:
        image.draft('RGB', (size * 2, size * 2))
        return np.asarray(image.convert('RGBA').resize(
            (size, size), Image.LANCZOS, reducing_gap=2.0), dtype=np.uint8)

class DecalsMixin:
    """``draw_decals`` and the GL objects it owns."""

    def _init_decals(self):
        self._decal_vao = 0
        self._decal_vbo = 0
        self._decal_capacity = 0
        self._decal_textures = 0
        self._decal_layers = 0
        self._decal_images_version = None
        #: Each layer's pixels by name, kept between rebuilds: new words on
        #: a sign draw that one layer, not every image again (decoding the
        #: wet masks alone is half a second). Image files are read afresh
        #: whenever the list of files changes.
        self._decal_layer_pixels = {}
        self._decal_layer_files = None
        self._decal_layer_names = None
        self._decal_ranges = (0.0, 1.0, 0.0, 1.0)
        self._decal_staging = np.zeros((0, _INSTANCE_FLOATS), dtype=np.float32)
        self._decal_base = None
        #: The emissive decals' images at full resolution: a texture array
        #: of just the layers emissive decals paint, the shared layer each
        #: holds (``_decal_image_index``: shared layer -> its layer here), and
        #: what it was made for (the image list's version, those layers).
        self._decal_image_textures = 0
        self._decal_image_key = None
        self._decal_image_index = {}
        self._decal_image_lut = _NO_LAYERS
        self._decal_image_pixels = {}
        self._decal_image_names = {}
        self._decal_image_side = DECAL_TEXTURE_SIZE
        self._decal_image_scales = np.ones((1, 2), dtype=np.float32)
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
                                 'decalFadeEnd', 'imageLayerScale[0]', 'useLayerScale')}
        return program

    def _decal_texture_array(self):
        """Every decal image as one texture array, in
        ``engine.decal_textures`` order; rebuilt when that list changes."""
        version, names = decal_textures.images()
        if version == self._decal_images_version:
            return self._decal_textures
        from PIL import Image
        self._decal_images_version = version
        self._decal_ranges = decal_textures.blood_ranges(names)
        layers = []
        size = DECAL_TEXTURE_SIZE
        blank = np.zeros((size, size, 4), dtype=np.uint8)
        files = tuple(n for n in names if not decal_text.is_entry(n))
        kept = self._decal_layer_pixels if files == self._decal_layer_files else {}
        pixels = {}
        # A sign's words are drawn only at full resolution, into the placed
        # images' array (it always paints from there): here a blank layer
        # keeps its place. The image files are decoded in parallel -- Pillow
        # releases the GIL while it decodes and resizes, and the whole
        # library (up to 1536 px sources) is read for the first frame that
        # shows a decal.
        wanted = [n for n in names if n not in kept and not decal_text.is_entry(n)]
        decoded = dict(zip(wanted, _decode_layers(wanted, size)))
        for name in names:
            layer = kept.get(name)
            if layer is None:
                layer = decoded.get(name)
            if layer is None:
                layer = blank
            pixels[name] = layer
            layers.append(layer)
        previous = self._decal_layer_names
        same_files = files == self._decal_layer_files
        self._decal_layer_pixels, self._decal_layer_files = pixels, files
        self._decal_layer_names = names
        if (self._decal_textures and same_files and previous is not None
                and len(previous) == len(names)):
            # Only signs' words changed (an edit, an IO input): their layers
            # here are blank either way (the words are in the placed images'
            # array), so the array stays as it is.
            return self._decal_textures
        if self._decal_textures:
            gl.glDeleteTextures(1, [self._decal_textures])
            self._decal_textures = 0
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

    def _decal_full_image(self, name):
        """Decal layer *name* as ``(PIL RGBA image, (width, height))`` at its
        own resolution (a sign's words: drawn at the largest emissive side),
        or None when it cannot be read."""
        from PIL import Image
        if decal_text.is_entry(name):
            side = EMISSIVE_TEXTURE_MAX
            return None, (side, side)
        try:
            image = Image.open(asset_source(os.path.join(DECAL_FOLDER, name)))
            image.load()
        except Exception:
            return None
        return image.convert('RGBA'), image.size

    def _decal_image_texture_array(self, layers):
        """The placed image decals' own texture array (plain and emissive),
        holding shared *layers* (sorted, unique) at their images' full
        resolution; made again only when the layers or the image list
        change. 0 when it cannot be made (they then paint from the shared
        array)."""
        key = (self._decal_images_version, tuple(layers))
        if key == self._decal_image_key:
            return self._decal_image_textures
        if self._update_sign_layers(key):
            return self._decal_image_textures
        if self._decal_image_textures:
            gl.glDeleteTextures(1, [self._decal_image_textures])
            self._decal_image_textures = 0
        self._decal_image_key, self._decal_image_index = key, {}
        self._decal_image_lut = _NO_LAYERS
        names = self._decal_layer_names or ()
        found = []
        for layer in layers:
            if not 0 <= layer < len(names):
                continue
            image = self._decal_full_image(names[layer])
            if image is not None:
                found.append((layer, names[layer], image))
        if not found:
            return 0
        side = budget_side(emissive_side(size for _l, _n, (_i, size) in found), len(found))
        cache = self._decal_image_pixels
        kept = {}
        data = []
        from PIL import Image
        scales = []
        for layer, name, (image, _size) in found:
            # Unstretched (its aspect kept, from the layer's corner, the rest
            # its edge pixels repeated) while there is a scale for it;
            # stretched to the layer past that, or a sign's square words.
            unstretched = image is not None and len(data) < IMAGE_LAYER_SCALES
            cached = cache.get((name, side, unstretched))
            if cached is None:
                try:
                    if image is None:
                        cached = (decal_text.layer_pixels(name, side), (1.0, 1.0))
                    elif unstretched:
                        cached = _unstretched_layer(image, side)
                    else:
                        cached = (np.asarray(image.resize((side, side), Image.LANCZOS),
                                             dtype=np.uint8), (1.0, 1.0))
                except Exception as exc:
                    print(f'[Renderer] decal image {name} not loaded at full size: {exc}')
                    continue
            kept[(name, side, unstretched)] = cached
            self._decal_image_index[layer] = len(data)
            data.append(cached[0])
            scales.append(cached[1])
        self._decal_image_scales = np.asarray(
            scales[:IMAGE_LAYER_SCALES] or [(1.0, 1.0)], dtype=np.float32)
        self._decal_image_pixels = kept
        index = self._decal_image_index
        lut = np.full(max(index, default=-1) + 1, -1, dtype=np.int64)
        for shared_layer, own_layer in index.items():
            lut[shared_layer] = own_layer
        self._decal_image_lut = lut
        if not data:
            return 0
        self._decal_image_names = {layer: names[layer] for layer in self._decal_image_index}
        self._decal_image_side = side
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        # Every level from _mip_chain, as _update_sign_layers rewrites a
        # sign's layer: a sign updated in place is then the same texels as
        # the array made afresh, whatever filter the driver's
        # glGenerateMipmap would have used.
        for level, stacked in enumerate(zip(*(_mip_chain(layer) for layer in data))):
            stacked = np.ascontiguousarray(np.stack(stacked), dtype=np.uint8)
            edge = stacked.shape[1]
            gl.glTexImage3D(gl.GL_TEXTURE_2D_ARRAY, level, gl.GL_RGBA8, edge, edge, len(data),
                            0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, stacked)
        for pname, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR),
                             (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                             (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                             (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D_ARRAY, pname, value)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        self._decal_image_textures = texture
        return texture

    def _update_sign_layers(self, key):
        """Only signs' words changed since the placed images' array was made
        (an edit, an IO input -- the same layers, each still in it): redraw
        those layers in place, with their own mipmaps, instead of the whole
        array. False when anything else changed."""
        texture = self._decal_image_textures
        held = getattr(self, '_decal_image_names', None)
        if not texture or not held or self._decal_image_key is None:
            return False
        layers = key[1]
        if tuple(layers) != tuple(self._decal_image_key[1]) or set(held) != set(layers):
            return False
        names = self._decal_layer_names or ()
        changed = []
        for layer in layers:
            if not 0 <= layer < len(names):
                return False
            was, now = held[layer], names[layer]
            if was == now:
                continue
            if not (decal_text.is_entry(was) and decal_text.is_entry(now)):
                return False
            changed.append(layer)
        side = self._decal_image_side
        cache = self._decal_image_pixels
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, texture)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
        for layer in changed:
            pixels = decal_text.layer_pixels(names[layer], side)
            cache.pop((held[layer], side, False), None)
            cache[(names[layer], side, False)] = (pixels, (1.0, 1.0))
            held[layer] = names[layer]
            index = self._decal_image_index[layer]
            for level, data in enumerate(_mip_chain(pixels)):
                edge = data.shape[0]
                gl.glTexSubImage3D(gl.GL_TEXTURE_2D_ARRAY, level, 0, 0, index,
                                   edge, edge, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)
        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
        self._decal_image_key = key
        return True

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
        viewport = getattr(self, '_frame_viewport', None) or gl.glGetIntegerv(gl.GL_VIEWPORT)
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
        # Placed images first, then blood over them (each in its order),
        # then the emissive images -- painted over the stained surface, and
        # in the frame the wet patches reflect -- and the wet ones last,
        # drawn by their own pass. The placed images, plain and emissive,
        # paint from their own full-resolution array; blood and wet masks
        # from the shared one.
        kinds = params[keep, 2]
        wet = kinds > 2.5
        glow = (params[keep, 3] > 7.5) & ~wet
        image = (np.abs(kinds - IMAGE) < 0.5) & ~glow
        blood = ~(wet | glow | image)
        order = np.concatenate((keep[image], keep[blood], keep[glow], keep[wet]))
        wet_count = int(wet.sum())
        glow_count = int(glow.sum())
        image_count = int(image.sum())
        plain_count = count - wet_count - glow_count
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
        image_texture = 0
        if image_count or glow_count:
            # Placed images paint from their images at full resolution: their
            # rows name their layer in that array instead. The array holds
            # every image the map's placed decals use, on screen or not, so
            # turning the camera does not make it again.
            placed = np.abs(params[:, 2] - IMAGE) < 0.5
            image_texture = self._decal_image_texture_array(
                glow_layers(params[placed, 0].astype(np.int64)))
            if image_texture:
                for start, rows in ((0, image_count), (plain_count, glow_count)):
                    if rows:
                        block = staging[start:start + rows]
                        remap_glow_rows(block, block[:, 16].astype(np.int64),
                                        self._decal_image_index, self._decal_image_lut)
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
            if image_texture and u['imageLayerScale[0]'] != -1:
                scales = self._decal_image_scales
                gl.glUniform2fv(u['imageLayerScale[0]'], len(scales), scales)
            if image_count and image_texture:
                # Placed images, from their full-resolution array; blood
                # (below) from the shared one.
                gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, image_texture)
                gl.glUniform1i(u['useLayerScale'], 1)
                self._decal_point(0)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, image_count)
                self.render_stats.draw_calls += 1
                gl.glUniform1i(u['useLayerScale'], 0)
                gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
                first_plain = image_count
            else:
                first_plain = 0
            if plain_count - first_plain:
                self._decal_point(first_plain)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, plain_count - first_plain)
                self.render_stats.draw_calls += 1
            if glow_count:
                # Emissive decals: their own colours, over what is there.
                gl.glUniform1i(u['wetPass'], 2)
                gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                if image_texture:
                    gl.glActiveTexture(gl.GL_TEXTURE0)
                    gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, image_texture)
                    gl.glUniform1i(u['useLayerScale'], 1)
                self._decal_point(plain_count)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, glow_count)
                self.render_stats.draw_calls += 1
                if image_texture:
                    gl.glUniform1i(u['useLayerScale'], 0)
                    gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
            if wet_count:
                # Wet patches: over a copy of the frame so far (blood and
                # images included), drawn plainly over what is there.
                if self._capture_glass_scene() is not None:
                    # Glossy reflections read a blurred copy: its mip chain,
                    # made now (the capture left the copy bound on its unit),
                    # and plain filtering again after the draw, so the glass
                    # and the water still read level 0 alone.
                    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
                    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                                       gl.GL_LINEAR_MIPMAP_LINEAR)
                    try:
                        gl.glActiveTexture(gl.GL_TEXTURE0)
                        gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._decal_textures)
                        gl.glBindVertexArray(self._decal_vao)
                        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._decal_vbo)
                        gl.glUseProgram(program)
                        gl.glUniform1i(u['wetPass'], 1)
                        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
                        self._decal_point(plain_count + glow_count)
                        gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, wet_count)
                        self.render_stats.draw_calls += 1
                    finally:
                        gl.glActiveTexture(gl.GL_TEXTURE0 + self._glass_scene_texture_unit)
                        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
                        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                                           gl.GL_LINEAR)
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
        if self._decal_image_textures:
            gl.glDeleteTextures(1, [self._decal_image_textures])
        self._decal_vao = self._decal_vbo = self._decal_textures = 0
        self._decal_image_textures = 0
        self._decal_image_key = None
        self._decal_image_index, self._decal_image_pixels = {}, {}
        self._decal_image_names = {}
        self._decal_image_lut = _NO_LAYERS
        self._decal_capacity = 0
        self._decal_base = None
        self._decal_layers = 0
        self._decal_layer_pixels, self._decal_layer_files = {}, None
        self._decal_layer_names = None
