"""The decal images: one sorted list both sides of a frame agree on.

Every image (``.png``, ``.jpg``, ``.jpeg``) in ``assets/textures/decals`` --
loose, archived or in an end user's ``.fiopak`` pack -- is a layer of the renderer's decal texture array,
in this order. The logic side names a placed decal's image by its layer
(:func:`layer_of`); blood picks a variant within the wall or floor range
(:func:`blood_ranges`). The list is read once and kept; a placed decal that
names an image the list does not have yet re-reads it (:func:`refresh`), and
the renderer rebuilds its array when :func:`images` reports a new version.

After the image files come the text layers (``engine.decal_text``): one per
different text a ``Decal`` paints, set by the decal table each time the
decals change (:func:`use_texts`), so a sign's words are a layer like any
image's.
"""

from __future__ import annotations

import os
import threading

DECAL_FOLDER = os.path.join('assets', 'textures', 'decals')
#: The image files a decal can paint. A JPEG has no transparency: it paints
#: its whole rectangle (a sign's plate, say).
IMAGE_SUFFIXES = ('.png', '.jpg', '.jpeg')

_lock = threading.Lock()
_state = {'version': 0, 'names': None, 'where': None, 'texts': (), 'all': None,
          'layout': 0}
#: A source set by :func:`use_source` (the Android player: its images come
#: from the played package, not a project's assets folder), or None.
_source = {'source': None}


class DecalSource:
    """Decal images that are not in a project's ``assets`` folder: *images*
    maps each file name to a callable returning its bytes (or None); *fonts*
    does the same for the font files a decal's text may be drawn in, by
    their paths under ``assets/fonts``."""

    def __init__(self, images, fonts=None):
        self.images = dict(images)
        self.fonts = dict(fonts or {})

    def names(self):
        return tuple(sorted((n for n in self.images if n.lower().endswith(IMAGE_SUFFIXES)),
                            key=str.casefold))

    def read(self, name):
        reader = self.images.get(name)
        return reader() if reader is not None else None

    def font_files(self):
        return sorted(self.fonts, key=str.casefold)

    def read_font(self, path):
        reader = self.fonts.get(path)
        return reader() if reader is not None else None


def use_source(source):
    """Take the decal images from *source* (a :class:`DecalSource`) instead
    of ``assets/textures/decals``; None goes back to the folder. The logic
    side (a placed decal's layer, blood's ranges) and the renderer (the
    texture array) both read the list from here, so they agree."""
    with _lock:
        _source['source'] = source
    return refresh()


def current_source():
    """The :class:`DecalSource` set by :func:`use_source`, or None."""
    return _source['source']


def read_image(name):
    """The bytes of decal image *name*, from the source or the folder; None
    when it cannot be read."""
    source = _source['source']
    if source is not None:
        return source.read(name)
    from engine.asset_archives import asset_source
    from engine.asset_paths import confined_path, warn_refused
    # The name comes from the map (an emissive decal's image): it may only
    # name a file inside the decal folder, never an absolute, '..' or UNC path.
    path = confined_path(DECAL_FOLDER, name)
    if path is None:
        warn_refused(name, 'decal image')
        return None
    try:
        found = asset_source(path)
        if hasattr(found, 'read'):
            return found.read()
        with open(found, 'rb') as handle:
            return handle.read()
    except (OSError, TypeError, ValueError):
        return None


_average = {}


def dominant_colour(name):
    """The main colour of decal image *name*, ``(r, g, b)`` in 0..1.

    What an emissive decal glows: the colour most of its opaque area is --
    a yellow sign with black lettering is yellow, not the darker yellow an
    average would give. Pixels are weighed by how opaque they are and sorted
    into coarse colour bins (8 levels a channel); the answer is the mean of
    the fullest bin. White for no image, or one that cannot be read. Worked
    out once per image and project (the image is read small, not at full
    size), and only when a decal asks: a map with no emissive decal never
    decodes anything here.
    """
    white = (1.0, 1.0, 1.0)
    if not name:
        return white
    key = (_where(), str(name))
    cached = _average.get(key)
    if cached is not None:
        return cached
    colour = white
    data = read_image(str(name))
    if data:
        try:
            import io
            import numpy as np
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                image.draft('RGB', (64, 64))          # a JPEG decodes small
                image = image.convert('RGBA')
                image.thumbnail((64, 64))
                pixels = np.asarray(image, dtype=np.uint8).reshape(-1, 4)
            alpha = pixels[:, 3].astype(np.float64) / 255.0
            if float(alpha.sum()) > 1e-6:
                rgb = pixels[:, :3].astype(np.int64)
                bins = (rgb[:, 0] >> 5) * 64 + (rgb[:, 1] >> 5) * 8 + (rgb[:, 2] >> 5)
                fullest = int(np.bincount(bins, weights=alpha, minlength=512).argmax())
                chosen = bins == fullest
                weight = alpha[chosen]
                mean = (rgb[chosen] * weight[:, None]).sum(axis=0) / (weight.sum() * 255.0)
                colour = tuple(float(min(max(v, 0.0), 1.0)) for v in mean)
        except Exception:
            colour = white
    with _lock:
        if len(_average) > 512:                       # other projects' images
            _average.clear()
        _average[key] = colour
    return colour


def _where():
    """The project the list belongs to: the asset index's root and the
    working directory a relative asset path is read from (or the source)."""
    import os
    source = _source['source']
    if source is not None:
        return ('source', id(source))
    from engine import asset_archives
    return (asset_archives._state.get('root'), os.getcwd())


def _read():
    source = _source['source']
    if source is not None:
        return source.names()
    from engine.asset_archives import asset_listdir
    return tuple(sorted((n for n in asset_listdir(DECAL_FOLDER)
                         if n.lower().endswith(IMAGE_SUFFIXES)), key=str.casefold))


def images():
    """``(version, names)``: the decal images in layer order, then the text
    layers -- the folder read again when the project changes (another map's
    folder, a test's)."""
    where = _where()
    with _lock:
        if _state['names'] is not None and _state['where'] == where:
            return _state['version'], _state['all']
    return refresh()


def _publish():
    """The list as it now stands (the lock held): a new version when it
    differs from the last one handed out."""
    combined = _state['names'] + _state['texts']
    if combined != _state['all']:
        _state['all'] = combined
        _state['version'] += 1
    return _state['version'], _state['all']


def refresh():
    """Read the folder again (an image was added); returns ``images()``."""
    _average.clear()
    where = _where()
    names = _read()
    with _lock:
        _state['where'] = where
        if names != _state['names']:
            _state['layout'] += 1               # every later layer may move
        _state['names'] = names
        return _publish()


def layout():
    """A number that changes whenever a layer already handed out may now be
    another image's (the files changed, or the text layers were tidied):
    then every row naming a layer is remade. New words for a sign do not
    change it -- they take the layer their old words freed."""
    return _state['layout']


#: How many freed text layers are kept for new words to take before the
#: text layers are tidied up (when they are also over half of them).
_FREE_KEPT = 8


def use_texts(entries):
    """Make the text layers *entries* (``engine.decal_text.entry`` of each
    decal with text, any order, repeats allowed); returns ``images()``.

    They come after the image files, and each keeps its layer for as long as
    it is wanted: new words take a layer freed by words no longer wanted, so
    a sign whose words change (an edit, an IO input) changes that one layer
    and no other sign's moves (:func:`layout` stays). Freed layers are blank
    until taken; when too many are, the text layers are packed again."""
    from engine.decal_text import FREE
    wanted = set(entries)
    wanted.discard(None)
    version, _names = images()
    with _lock:
        texts = list(_state['texts'])
        if set(texts) - {FREE} == wanted:
            return _state['version'], _state['all']
        if not wanted:
            slots = []                          # no sign left names a text layer
        else:
            slots = [t if t in wanted else FREE for t in texts]
            new = sorted(wanted - set(slots))
            free = [i for i, t in enumerate(slots) if t == FREE]
            for i, entry in zip(free, new):
                slots[i] = entry
            slots.extend(new[len(free):])
            unused = slots.count(FREE)
            if unused > _FREE_KEPT and unused * 2 > len(slots):
                slots = sorted(wanted)
                _state['layout'] += 1           # packed: the signs' layers move
        _state['texts'] = tuple(slots)
        return _publish()


def is_wet_image(name):
    """Whether decal image *name* is a wet mask: its file name has ``wet`` or
    ``puddle`` in it (any case). A decal of one is a Wet decal -- the image
    says where the surface is wet -- and any other is an Image decal."""
    base = os.path.basename(str(name or '').replace('\\', '/')).casefold()
    return 'wet' in base or 'puddle' in base


def layer_of(name, names=None):
    """The layer of decal image *name* (a file name or a path ending in
    one; any case), or -1."""
    if not name:
        return -1
    if names is None:
        names = images()[1]
    base = os.path.basename(str(name).replace('\\', '/')).casefold()
    for index, candidate in enumerate(names):
        if candidate.casefold() == base:
            return index
    return -1


def blood_ranges(names):
    """``(first wall layer, wall count, first floor layer, floor count)``.

    With only one kind of blood image (or files named neither), both kinds
    draw from every blood image.
    """
    def run(prefix):
        rows = [i for i, n in enumerate(names) if n.lower().startswith(prefix)]
        return (float(rows[0]), float(len(rows))) if rows else (0.0, 0.0)
    walls, floors = run('blood_wall_'), run('blood_floor_')
    if not walls[1] or not floors[1]:
        both = run('blood_')
        if not both[1]:
            # No blood images at all: any image file, never a sign's text.
            from engine.decal_text import is_entry
            both = (0.0, float(sum(1 for n in names if not is_entry(n))))
        return both + both
    return walls + floors
