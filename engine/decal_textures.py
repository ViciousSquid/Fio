"""The decal images: one sorted list both sides of a frame agree on.

Every ``.png`` in ``assets/textures/decals`` -- loose, archived or in an end
user's ``.fiopak`` pack -- is a layer of the renderer's decal texture array,
in this order. The logic side names a placed decal's image by its layer
(:func:`layer_of`); blood picks a variant within the wall or floor range
(:func:`blood_ranges`). The list is read once and kept; a placed decal that
names an image the list does not have yet re-reads it (:func:`refresh`), and
the renderer rebuilds its array when :func:`images` reports a new version.
"""

from __future__ import annotations

import os
import threading

DECAL_FOLDER = os.path.join('assets', 'textures', 'decals')

_lock = threading.Lock()
_state = {'version': 0, 'names': None, 'where': None}
#: A source set by :func:`use_source` (the Android player: its images come
#: from the played package, not a project's assets folder), or None.
_source = {'source': None}


class DecalSource:
    """Decal images that are not in a project's ``assets`` folder: *images*
    maps each file name to a callable returning its bytes (or None)."""

    def __init__(self, images):
        self.images = dict(images)

    def names(self):
        return tuple(sorted((n for n in self.images if n.lower().endswith('.png')),
                            key=str.casefold))

    def read(self, name):
        reader = self.images.get(name)
        return reader() if reader is not None else None


def use_source(source):
    """Take the decal images from *source* (a :class:`DecalSource`) instead
    of ``assets/textures/decals``; None goes back to the folder. The logic
    side (a placed decal's layer, blood's ranges) and the renderer (the
    texture array) both read the list from here, so they agree."""
    with _lock:
        _source['source'] = source
    return refresh()


def read_image(name):
    """The bytes of decal image *name*, from the source or the folder; None
    when it cannot be read."""
    source = _source['source']
    if source is not None:
        return source.read(name)
    from engine.asset_archives import asset_source
    try:
        found = asset_source(os.path.join(DECAL_FOLDER, name))
        if hasattr(found, 'read'):
            return found.read()
        with open(found, 'rb') as handle:
            return handle.read()
    except (OSError, TypeError, ValueError):
        return None


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
                         if n.lower().endswith('.png')), key=str.casefold))


def images():
    """``(version, names)``: the decal images in layer order -- read again
    when the project changes (another map's folder, a test's)."""
    where = _where()
    with _lock:
        if _state['names'] is not None and _state['where'] == where:
            return _state['version'], _state['names']
    return refresh()


def refresh():
    """Read the folder again (an image was added); returns ``images()``."""
    where = _where()
    names = _read()
    with _lock:
        _state['where'] = where
        if names != _state['names']:
            _state['names'] = names
            _state['version'] += 1
        return _state['version'], _state['names']


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
            both = (0.0, float(len(names)))
        return both + both
    return walls + floors
