"""Companion material maps for the HD renderer, and how to make them.

In HD every diffuse texture has up to three files beside it::

    <name>_n.<ext>     tangent-space normal map, OpenGL convention (+Y / green
                       is up), with the surface HEIGHT in alpha (255 = highest)
    <name>_m.<ext>     R = ambient occlusion, G = roughness, B = metalness,
                       A = 1 - emission (255 = not emissive)
    <name>.mat.json    the texture's parameters (the "sidecar")

``<ext>`` is ``png``, ``webp`` or ``jpg``, looked for in that order.  The
diffuse image itself is never modified, renamed or resized, and the companions
have its resolution.

Most textures have no painted maps, so this module can *derive* a plausible
pair from the colour image alone (:func:`generate`): a height from the
luminance, a normal from that height's slope, occlusion from its cavities and
a roughness from luminance and local contrast.  The same generator serves two
callers:

* ``tools/make_material_maps.py`` bakes the maps for the stock assets and
  records, in the sidecar's ``"generated"`` block, which generator version and
  which source image they came from.  Maps beside a sidecar *without* that
  block are hand-made, and nothing here ever overwrites them.
* The renderer's fallback: a texture with no companions gets them generated
  at load and kept in a per-user cache (:func:`ensure_cached`), keyed by the
  image's content, the parameters and :data:`GENERATOR_VERSION`.

Textures tile, so every filter here wraps around: the maps of a tiling texture
tile too, and shifting the input shifts the output by exactly the same amount.
Nothing is random and nothing depends on the clock: the same image and
parameters always give the same arrays.

Pure NumPy and Pillow: no OpenGL and no Qt, so the tool, the tests and the
Android player can all import it.
"""

import fnmatch
import hashlib
import io
import json
import os
import sys

import numpy as np

#: Bump when :func:`generate` changes its output: every cached and every baked
#: map is then stale.
GENERATOR_VERSION = 1

#: Companion file extensions, in the order the renderer looks for them.
COMPANION_EXTENSIONS = ('.png', '.webp', '.jpg')

#: Image files that can be a diffuse texture.
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tga', '.ppm', '.gif')

#: Stem suffixes of files that are companions, not textures of their own:
#: normal, material, stand-alone height and emission mask.
COMPANION_SUFFIXES = ('_n', '_m', '_h', '_e')

SIDECAR_SUFFIX = '.mat.json'

DEFAULT_PARAMS = {
    # -- generator inputs --------------------------------------------------
    # How steep the normal map's slopes are for a given height difference.
    'height_strength': 1.0,
    # True when dark means high (dark rivets on a pale plate).
    'invert': False,
    # Weight of the high-pass detail against the broad luminance in the
    # height: 0 = only the large shapes, 1 = only the fine grain.
    'detail': 0.6,
    # Roughness of a texel of average brightness, and how far luminance and
    # local contrast swing it either way.
    'roughness_base': 0.7,
    'roughness_range': 0.25,
    # Metalness, constant over the texture (0..1).
    'metal': 0.0,
    # None = nothing emits.  Otherwise the brightness (0..1) above which a
    # texel emits; see generate() for what "brightness" is.
    'emission_threshold': None,
    # File name of a greyscale mask beside the texture (white emits).  Used
    # instead of the threshold when it names a readable image.
    'emission_mask': None,
    # How much the cavities darken the occlusion channel (0 = none).
    'ao_strength': 1.0,
    # -- read by the renderer, not by the generator -------------------------
    # Emission gain.
    'emission': 1.0,
    # Relief depth in world units for parallax mapping; 0 = none.
    'parallax': 1.5,
}

#: The parameters that change what generate() returns.  ``emission`` and
#: ``parallax`` are applied by the renderer, so tuning them invalidates nothing.
GENERATOR_KEYS = ('height_strength', 'invert', 'detail', 'roughness_base',
                  'roughness_range', 'metal', 'emission_threshold',
                  'emission_mask', 'ao_strength')

#: What a tool texture (and anything without maps in a non-HD fallback) uses:
#: a flat normal at full height, no occlusion, fully rough, not metal, dark.
FLAT_VALUES = {
    'normal': (128, 128, 255),
    'height': 255,
    'ao': 1.0,
    'roughness': 1.0,
    'metal': 0.0,
    'emission': 0.0,
    'parallax': 0.0,
}

# Slope of the normal per unit of height difference between neighbouring
# texels, at height_strength 1.
_SLOPE_SCALE = 6.0
# The height's range is stretched to fill 0..1, but never by more than this:
# an almost flat texture stays almost flat instead of having its noise blown
# up to full relief.
_MIN_HEIGHT_RANGE = 0.25
# Width, in brightness, of the ramp from "not emitting" to "fully emitting".
_EMISSION_RAMP = 0.12


# -- which files get maps ------------------------------------------------------

def _norm_rel(rel_path):
    """*rel_path* with forward slashes and no leading ``./`` or ``/``."""
    rel = str(rel_path).replace('\\', '/')
    while rel.startswith('./'):
        rel = rel[2:]
    return rel.lstrip('/')


def _stem_ext(name):
    stem, ext = os.path.splitext(name)
    return stem, ext.lower()


def is_tool_texture(rel_path):
    """Whether *rel_path* (relative to ``assets/textures``) is a tool texture.

    Tool textures are never seen lit in a finished level -- ``nodraw*``,
    ``trigger*``, the ``default.png`` placeholder, ``black.jpeg`` and the
    measuring grids under ``Dev/`` -- so they get flat values and no maps.
    """
    rel = _norm_rel(rel_path).lower()
    name = rel.rsplit('/', 1)[-1]
    if rel.startswith('dev/') or '/dev/' in '/' + rel:
        return True
    if name.startswith('nodraw') or name.startswith('trigger'):
        return True
    return name in ('default.png', 'black.jpeg')


def flat_values(rel_path):
    """The neutral values a tool texture renders with, or None for any other.

    The single source of what "no maps" means: a copy of :data:`FLAT_VALUES`.
    """
    if is_tool_texture(rel_path):
        return dict(FLAT_VALUES)
    return None


def is_companion(rel_path):
    """Whether the file is itself a companion map (``*_n.*``, ``*_m.*``,
    ``*_h.*``, or an ``*_e.*`` emission mask) or a sidecar."""
    name = _norm_rel(rel_path).rsplit('/', 1)[-1]
    if name.lower().endswith(SIDECAR_SUFFIX):
        return True
    stem, ext = _stem_ext(name)
    return ext in IMAGE_EXTENSIONS and stem.lower().endswith(COMPANION_SUFFIXES)


def is_surface_texture(rel_path):
    """Whether *rel_path* (relative to ``assets/textures``) can have maps.

    False for what never gets them: ``water_normal.png`` (already a normal
    map), light cookies and effect sprites (``cookies/``, ``effects/`` -- they
    stay non-PBR), files that are companions themselves, and anything that is
    not an image.  Tool textures are surface textures as far as this goes;
    ask :func:`is_tool_texture` about those.
    """
    rel = _norm_rel(rel_path)
    lower = rel.lower()
    name = lower.rsplit('/', 1)[-1]
    _stem, ext = _stem_ext(name)
    if ext not in IMAGE_EXTENSIONS or ext == '.gif':
        return False
    if is_companion(rel):
        return False
    if name == 'water_normal.png':
        return False
    folders = lower.split('/')[:-1]
    return 'cookies' not in folders and 'effects' not in folders


def _rel_to_textures(path):
    """*path* relative to the innermost ``textures`` folder above it, or just
    its file name when there is none (a model's texture, a loose file)."""
    parts = os.path.abspath(path).replace('\\', '/').split('/')
    for i in range(len(parts) - 2, -1, -1):
        if parts[i].lower() == 'textures':
            return '/'.join(parts[i + 1:])
    return parts[-1]


# -- companions and sidecars ---------------------------------------------------

def companion_paths(diffuse_path):
    """The companions that exist beside *diffuse_path*.

    A dict with the keys ``'normal'``, ``'material'``, ``'height'`` and
    ``'sidecar'``; each is the path of the existing file or None.  Maps are
    looked for as ``.png``, ``.webp`` then ``.jpg``.
    """
    base = os.path.splitext(diffuse_path)[0]
    found = {}
    for key, suffix in (('normal', '_n'), ('material', '_m'), ('height', '_h')):
        found[key] = None
        for ext in COMPANION_EXTENSIONS:
            candidate = base + suffix + ext
            if os.path.isfile(candidate):
                found[key] = candidate
                break
    sidecar = base + SIDECAR_SUFFIX
    found['sidecar'] = sidecar if os.path.isfile(sidecar) else None
    return found


def sidecar_path(diffuse_path):
    """Where *diffuse_path*'s sidecar lives, whether or not it exists."""
    return os.path.splitext(diffuse_path)[0] + SIDECAR_SUFFIX


def load_sidecar(path):
    """The sidecar's JSON object, or ``{}`` when it is missing or unreadable."""
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_params(path):
    """The parameters in the sidecar at *path*, over :data:`DEFAULT_PARAMS`.

    A missing or malformed file gives the defaults.  The ``"generated"``
    block is bookkeeping, not a parameter, and is left out; keys this version
    does not know are kept so a newer file survives a round trip.
    """
    params = dict(DEFAULT_PARAMS)
    for key, value in load_sidecar(path).items():
        if key != 'generated':
            params[key] = value
    return params


def generated_info(path):
    """The sidecar's ``"generated"`` block, or None for hand-made maps."""
    info = load_sidecar(path).get('generated')
    return info if isinstance(info, dict) else None


def _atomic_write(path, data):
    """Write *data* to *path* so that a reader never sees half a file."""
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    temp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(temp, 'wb') as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass


def save_params(path, params, generated_info=None):
    """Write the sidecar at *path*: *params* flat at the top level.

    *generated_info* -- ``{"version": ..., "source_sha256": ...}`` -- is
    written as the ``"generated"`` block when the maps came from the tool.
    Pass None for hand-made maps: leaving the block out is what marks them.
    """
    data = dict(DEFAULT_PARAMS)
    data.update({k: v for k, v in dict(params or {}).items() if k != 'generated'})
    if generated_info:
        data['generated'] = dict(generated_info)
    text = json.dumps(data, indent=2, sort_keys=True) + '\n'
    _atomic_write(path, text.encode('utf-8'))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def params_digest(params):
    """A hash of the generator inputs in *params* (canonical JSON)."""
    return sha256_bytes(_canonical_params(params).encode('utf-8'))


def is_current(diffuse_path):
    """Whether *diffuse_path*'s generated maps match the image and its sidecar.

    True only when both maps exist and the ``"generated"`` block names this
    :data:`GENERATOR_VERSION`, the image's present content and (when it was
    recorded) the parameters as they now stand.
    """
    found = companion_paths(diffuse_path)
    if not (found['normal'] and found['material'] and found['sidecar']):
        return False
    info = generated_info(found['sidecar'])
    if not info or info.get('version') != GENERATOR_VERSION:
        return False
    try:
        with open(diffuse_path, 'rb') as handle:
            digest = sha256_bytes(handle.read())
    except OSError:
        return False
    if info.get('source_sha256') != digest:
        return False
    recorded = info.get('params_sha256')
    return recorded is None or recorded == params_digest(load_params(found['sidecar']))


def status(diffuse_path):
    """How *diffuse_path* gets its material in HD.

    * ``'tool'`` -- a tool texture: flat values, no maps.
    * ``'hand-made'`` -- maps beside it and no ``"generated"`` block.
    * ``'generated'`` -- maps beside it that the tool baked.
    * ``'runtime'`` -- nothing committed: generated at load into the cache.

    None for a file that never gets maps at all (see
    :func:`is_surface_texture`).
    """
    rel = _rel_to_textures(diffuse_path)
    if is_tool_texture(rel):
        return 'tool'
    if not is_surface_texture(rel):
        return None
    found = companion_paths(diffuse_path)
    if found['normal'] or found['material']:
        return 'generated' if generated_info(found['sidecar']) else 'hand-made'
    return 'runtime'


# -- the generator -------------------------------------------------------------

def _blur(field, levels):
    """A wrap-around blur: *levels* passes of a 1-2-1 kernel whose taps move
    twice as far apart each pass (1, 2, 4, ... texels).

    Built from np.roll alone, so it wraps at the edges and treats every texel
    identically: rolling the input rolls the output, bit for bit.
    """
    for level in range(levels):
        step = 1 << level
        for axis in (0, 1):
            field = (np.roll(field, step, axis) + np.roll(field, -step, axis)
                     + field + field) * np.float32(0.25)
    return field


def _slope(height):
    """Sobel slopes of *height* along the columns and the rows, wrapping."""
    def smooth(field, axis):
        return (np.roll(field, 1, axis) + np.roll(field, -1, axis)
                + field + field) * np.float32(0.25)
    along_x = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) * np.float32(0.5)
    along_y = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) * np.float32(0.5)
    return smooth(along_x, 0), smooth(along_y, 1)


def _to_byte(field):
    return np.clip(np.rint(field * np.float32(255.0)), 0, 255).astype(np.uint8)


def _as_rgba(image):
    """*image* as an HxWx4 uint8 array (grey and RGB gain what is missing)."""
    array = np.asarray(image)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim == 2:
        array = array[:, :, None]
    if array.ndim != 3 or array.shape[2] not in (1, 2, 3, 4):
        raise ValueError('expected an HxWx3 or HxWx4 image, got shape %r' % (array.shape,))
    height, width, channels = array.shape
    if channels == 4:
        return array
    out = np.empty((height, width, 4), np.uint8)
    if channels >= 3:
        out[:, :, :3] = array[:, :, :3]
        out[:, :, 3] = 255
    else:
        out[:, :, :3] = array[:, :, :1]
        out[:, :, 3] = array[:, :, 1] if channels == 2 else 255
    return out


def _full_params(params):
    full = dict(DEFAULT_PARAMS)
    if params:
        full.update(params)
    return full


def generate(rgba, params=None, emission_mask=None):
    """Derive a normal map and a material map from a colour image.

    *rgba* is an HxWx4 (or HxWx3) uint8 array, top row first.  Returns
    ``(normal_rgba, material_rgba)``, both HxWx4 uint8, top row first:

    * normal: XYZ packed as ``n * 127 + 128`` in RGB (flat is 128, 128, 255;
      green is up, the OpenGL convention), the height in alpha.
    * material: R = occlusion, G = roughness, B = metalness,
      A = 255 - emission.

    How each comes about:

    * **Height** is the luminance (one minus it when ``invert``), split into
      the broad shapes and the fine grain and mixed by ``detail``, then
      stretched so the highest texel is 255.  Parallax pushes the rest *down*
      from there, so a flat image is 255 everywhere and has no relief.
    * **Normal** is the slope of that height, scaled by ``height_strength``.
    * **Occlusion** darkens texels lower than their surroundings.
    * **Roughness** is ``roughness_base``, swung by ``roughness_range``:
      texels darker than their surroundings and busy areas are rougher,
      highlights smoother.
    * **Metalness** is the constant ``metal``.
    * **Emission** is where the image is brighter than
      ``emission_threshold`` -- measured on the brightest colour channel, not
      the luminance, so a saturated blue or red lamp counts as bright -- or,
      when *emission_mask* (an HxW greyscale array) is given, that mask.

    A partly transparent image (a decal) is treated as flat where it is
    transparent, and emits nothing there.

    Every filter wraps around, so the result tiles when the input does.
    """
    p = _full_params(params)
    image = _as_rgba(rgba)
    rows, cols = image.shape[:2]
    if rows == 0 or cols == 0:
        empty = np.zeros((rows, cols, 4), np.uint8)
        return empty, empty.copy()

    channels = image.astype(np.int64)
    alpha_i = channels[:, :, 3]
    # Rec. 709 luminance in whole numbers (the weights sum to 256), so the
    # mean below is exact whatever order the texels are summed in.
    lum_i = 54 * channels[:, :, 0] + 183 * channels[:, :, 1] + 19 * channels[:, :, 2]
    lum = lum_i.astype(np.float32) / np.float32(256.0 * 255.0)

    opaque = bool(alpha_i.min() == 255)
    if not opaque:
        # What lies under transparent texels is arbitrary: fade it to the
        # visible part's mean so it reads as flat.
        coverage = alpha_i.astype(np.float32) / np.float32(255.0)
        weight = int(alpha_i.sum())
        mean = (float(int((lum_i * alpha_i).sum())) / (weight * 256.0 * 255.0)
                if weight else 0.5)
        lum = lum * coverage + np.float32(mean) * (np.float32(1.0) - coverage)
    else:
        coverage = None

    source = np.float32(1.0) - lum if p['invert'] else lum
    soft = _blur(source, 1)           # takes the edge off JPEG and scan noise
    broad = _blur(source, 5)          # the large shapes

    detail = np.float32(min(max(float(p['detail']), 0.0), 1.0))
    raw = (np.float32(1.0) - detail) * broad + detail * (
        np.float32(0.5) + (soft - broad) * np.float32(2.0))
    low, high = float(raw.min()), float(raw.max())
    scale = np.float32(1.0 / max(high - low, _MIN_HEIGHT_RANGE))
    height = np.clip(np.float32(1.0) - (np.float32(high) - raw) * scale, 0.0, 1.0)

    # Normal.  Rows run downwards and +Y is up, hence the sign of Y.
    slope_x, slope_y = _slope(height)
    strength = np.float32(float(p['height_strength']) * _SLOPE_SCALE)
    nx = -slope_x * strength
    ny = slope_y * strength
    inverse = np.float32(1.0) / np.sqrt(nx * nx + ny * ny + np.float32(1.0))
    normal = np.empty((rows, cols, 4), np.uint8)
    for index, component in enumerate((nx * inverse, ny * inverse, inverse)):
        normal[:, :, index] = np.clip(
            np.rint(component * np.float32(127.0) + np.float32(128.0)), 0, 255)
    normal[:, :, 3] = _to_byte(height)

    # Occlusion: how far a texel sits below its neighbourhood.
    cavity = np.clip((_blur(height, 4) - height) * np.float32(3.0), 0.0, 1.0)
    occlusion = np.float32(1.0) - np.float32(float(p['ao_strength'])) * cavity

    # Roughness: darker than the surroundings and busier means rougher.
    plain_soft = _blur(lum, 1) if p['invert'] else soft
    plain_broad = _blur(lum, 5) if p['invert'] else broad
    darker = plain_broad - plain_soft
    busy = _blur(np.abs(darker), 3)
    swing = np.clip(darker * np.float32(3.0) + busy * np.float32(2.0), -1.0, 1.0)
    roughness = np.clip(np.float32(float(p['roughness_base']))
                        + np.float32(float(p['roughness_range'])) * swing, 0.04, 1.0)

    # Emission.
    emission = None
    if emission_mask is not None:
        mask = np.asarray(emission_mask)
        if mask.ndim == 3:
            mask = mask[:, :, 0]
        if mask.shape == (rows, cols):
            emission = mask.astype(np.float32) / np.float32(255.0)
    if emission is None and p['emission_threshold'] is not None:
        threshold = np.float32(float(p['emission_threshold']))
        brightest = image[:, :, :3].max(axis=2).astype(np.float32) / np.float32(255.0)
        emission = np.clip((brightest - threshold) / np.float32(_EMISSION_RAMP), 0.0, 1.0)
    if emission is not None and coverage is not None:
        emission = emission * coverage

    material = np.empty((rows, cols, 4), np.uint8)
    material[:, :, 0] = _to_byte(np.clip(occlusion, 0.0, 1.0))
    material[:, :, 1] = _to_byte(roughness)
    material[:, :, 2] = int(round(min(max(float(p['metal']), 0.0), 1.0) * 255.0))
    material[:, :, 3] = 255 if emission is None else 255 - _to_byte(emission)
    return normal, material


# -- runtime cache -------------------------------------------------------------

def _canonical_params(params):
    full = _full_params(params)
    return json.dumps({key: full.get(key) for key in GENERATOR_KEYS},
                      sort_keys=True, separators=(',', ':'))


def cache_key(image_bytes, params=None, extra=b''):
    """The cache name for the maps of one image under one set of parameters.

    A SHA-256 over the image file's bytes, :data:`GENERATOR_VERSION` and the
    canonical JSON of the generator inputs in *params* (``emission`` and
    ``parallax`` are the renderer's, and do not count).  *extra* is the bytes
    of an emission mask, when there is one.
    """
    digest = hashlib.sha256()
    digest.update(b'fio-material-maps\0')
    digest.update(str(int(GENERATOR_VERSION)).encode('ascii') + b'\0')
    digest.update(_canonical_params(params).encode('utf-8') + b'\0')
    digest.update(hashlib.sha256(bytes(image_bytes)).digest())
    if extra:
        digest.update(hashlib.sha256(bytes(extra)).digest())
    return digest.hexdigest()


def _user_data_dir():
    """Fio's per-user data folder (the convention of editor/user_paths.py)."""
    try:
        from editor.user_paths import user_data_dir
        return user_data_dir()
    except ImportError:      # the player ships without the editor package
        home = os.path.expanduser('~')
        if sys.platform == 'darwin':
            base = os.path.join(home, 'Library', 'Application Support')
        elif sys.platform == 'win32':
            base = os.environ.get('APPDATA') or home
        else:
            base = os.environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
        return os.path.join(base, 'Fio')


def cache_dir():
    """Where runtime-generated maps are kept.

    ``$FIO_MATERIAL_CACHE`` when set; otherwise ``cache/materials`` in Fio's
    per-user data folder.  Never the install folder: the cache is disposable
    and per machine, and an install may be read-only.
    """
    override = os.environ.get('FIO_MATERIAL_CACHE')
    if override:
        return override
    return os.path.join(_user_data_dir(), 'cache', 'materials')


def _decode(image_bytes):
    """The image in *image_bytes* as an RGBA array, or None if unreadable."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(bytes(image_bytes))) as image:
            return np.asarray(image.convert('RGBA'))
    except Exception:
        return None


def _encode_png(array):
    from PIL import Image
    buffer = io.BytesIO()
    Image.fromarray(array, 'RGBA').save(buffer, format='PNG', compress_level=3)
    return buffer.getvalue()


def ensure_cached(diffuse_bytes, params=None, cache=None, mask_bytes=None):
    """The cached maps for an image, generating them first if need be.

    *diffuse_bytes* is the diffuse image file's content.  Returns
    ``(normal_path, material_path)`` -- ``<key>_n.png`` and ``<key>_m.png``
    in *cache* (default :func:`cache_dir`) -- writing them only when they are
    not there yet, each through a temporary file and ``os.replace`` so that
    two processes loading the same level cannot read a half-written map.

    *mask_bytes* is the content of the ``emission_mask`` image, if any.

    Never raises on an image it cannot read or a cache it cannot write:
    returns None, and the caller renders with flat values.
    """
    try:
        folder = cache or cache_dir()
        key = cache_key(diffuse_bytes, params, mask_bytes or b'')
        normal_path = os.path.join(folder, key + '_n.png')
        material_path = os.path.join(folder, key + '_m.png')
        if os.path.isfile(normal_path) and os.path.isfile(material_path):
            return normal_path, material_path
        rgba = _decode(diffuse_bytes)
        if rgba is None:
            return None
        mask = None
        if mask_bytes:
            decoded = _decode(mask_bytes)
            if decoded is not None:
                mask = decoded[:, :, 0]
        normal, material = generate(rgba, params, emission_mask=mask)
        _atomic_write(normal_path, _encode_png(normal))
        _atomic_write(material_path, _encode_png(material))
        return normal_path, material_path
    except Exception:
        return None


def matches_any(rel_path, patterns):
    """Whether *rel_path* or its file name matches one of the glob *patterns*."""
    rel = _norm_rel(rel_path)
    name = rel.rsplit('/', 1)[-1]
    return any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern)
               for pattern in patterns)
