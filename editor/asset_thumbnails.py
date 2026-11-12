"""Asset Browser thumbnails: small images, decoded once per version of a file.

A tile needs at most a 90-pixel picture, but a texture is often 1024 or 2048
pixels square: holding that as each tile's pixmap cost 4-16 MB a tile, and
every folder visit (or size change, or file-watcher refresh) decoded every
file again in full. Instead each asset's thumbnail is decoded once, at no
more than :data:`THUMBNAIL_EDGE` pixels (``QImageReader`` scales while it
decodes where the format allows -- JPEG does), and kept as a ``QImage``
keyed by the asset's path and :func:`engine.asset_archives.asset_stamp`: an
edited, replaced or repacked file is decoded afresh, an unchanged one never.
Tiles turn the small image into a pixmap of their own size.

The cache is bounded by the bytes its images hold (:data:`CACHE_BYTES`,
100 MB; least recently used out). A 90x90 RGBA image is ~32 KB, so that is
about 3,200 square thumbnails -- more when they are not square. A file
pushed out is decoded again, small, the next time a tile needs it.
"""

from __future__ import annotations

from collections import OrderedDict
import os

from engine.asset_archives import asset_bytes, asset_source, asset_stamp

#: The largest tile's thumbnail edge (THUMBNAIL_SIZES['medium']); smaller
#: tiles scale the cached image down.
THUMBNAIL_EDGE = 90
#: A texture this many times wider than it is tall (or taller than wide)
#: takes two grid slots across (or down): 2:1 takes two, 4:3 one.
WIDE = 1.5

#: Bytes of image data kept; the least recently used goes first.
CACHE_BYTES = 100 * 1024 * 1024

#: normalised path -> (stamp, QImage). One entry a path: a newer version of
#: a file replaces the old one's image rather than sitting beside it.
_cache: "OrderedDict[str, tuple]" = OrderedDict()
#: The bytes the cached images hold (QImage.sizeInBytes, summed).
_held = [0]
#: Decodes performed (tests: a second visit must not decode again).
stats = {'decoded': 0, 'hits': 0}


def _key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def spans(width, height):
    """``(across, down)``: the grid slots a *width* x *height* texture
    takes -- (2, 1) for a wide one, (1, 2) for a tall one, else (1, 1)."""
    if width <= 0 or height <= 0:
        return (1, 1)
    ratio = width / height
    if ratio >= WIDE:
        return (2, 1)
    if ratio <= 1.0 / WIDE:
        return (1, 2)
    return (1, 1)


def fit_box(width, height, edge=THUMBNAIL_EDGE):
    """The box a *width* x *height* image's thumbnail fits in: *edge* per
    slot it takes, so a wide texture keeps its detail across two."""
    across, down = spans(width, height)
    return edge * across, edge * down


def shrink(image, edge=THUMBNAIL_EDGE):
    """*image* fitted inside its :func:`fit_box` (aspect kept); as it is
    when it already fits or is null."""
    from PyQt5.QtCore import Qt
    if image.isNull():
        return image
    box_w, box_h = fit_box(image.width(), image.height(), edge)
    if image.width() <= box_w and image.height() <= box_h:
        return image
    return image.scaled(box_w, box_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def lookup(path, edge=THUMBNAIL_EDGE, stamp=None):
    """The cached thumbnail of *path* while the asset is unchanged (its
    :func:`~engine.asset_archives.asset_stamp` is the one it was made
    from), else None. *stamp*: the asset's stamp when the caller has it.
    Main thread only, as :func:`store` is."""
    key = _key(path)
    held = _cache.get(key)
    if held is None:
        return None
    if stamp is None:
        stamp = asset_stamp(path)
    if stamp is None or held[0] != (stamp, edge):
        return None
    _cache.move_to_end(key)
    stats['hits'] += 1
    return held[1]


def store(path, stamp, image, edge=THUMBNAIL_EDGE):
    """Keep *image* (a QImage, a QPixmap or None; shrunk to *edge*) as the
    thumbnail of *path* at *stamp*, dropping the least recently used past
    :data:`CACHE_BYTES`; returns the image kept. A null image is kept too,
    so an unreadable file is not tried again until it changes; with no
    *stamp* (the asset exists nowhere) nothing is kept."""
    from PyQt5.QtGui import QImage, QPixmap
    if isinstance(image, QPixmap):
        image = image.toImage()
    image = shrink(image, edge) if isinstance(image, QImage) else QImage()
    key = _key(path)
    _forget(key)
    if stamp is not None:
        _cache[key] = ((stamp, edge), image)
        _held[0] += image.sizeInBytes()
        while _held[0] > CACHE_BYTES and len(_cache) > 1:
            _forget(next(iter(_cache)))
    return image


def cached(path, make, edge=THUMBNAIL_EDGE):
    """The thumbnail ``QImage`` of the asset at *path*: the cached one while
    the asset is unchanged, else ``make()`` (a QImage, a QPixmap or None),
    shrunk to *edge* and kept (see :func:`store`). Synchronous: the Asset
    Browser decodes on worker threads instead (``decode_image`` there,
    :func:`store` here)."""
    stamp = asset_stamp(path)
    image = lookup(path, edge, stamp)
    if image is not None:
        return image
    made = make()
    stats['decoded'] += 1
    return store(path, stamp, made, edge)


def _forget(key):
    held = _cache.pop(key, None)
    if held is not None:
        _held[0] -= held[1].sizeInBytes()


def held_bytes():
    """The bytes the cached thumbnails hold now."""
    return _held[0]


def decode_image(path, edge=THUMBNAIL_EDGE):
    """The image asset at *path* (loose or archived), decoded at no more
    than *edge* pixels a side; null when it cannot be read."""
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PyQt5.QtGui import QImage, QImageReader
    full = os.fspath(path)
    buffer = None
    if os.path.isfile(full):
        reader = QImageReader(full)
    else:
        data = asset_bytes(full)
        if data is None:
            return QImage()
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
    size = reader.size()
    box_w, box_h = fit_box(size.width(), size.height(), edge) if size.isValid() else (edge, edge)
    if size.isValid() and (size.width() > box_w or size.height() > box_h):
        target = size.scaled(box_w, box_h, Qt.KeepAspectRatio)
        target.setWidth(max(1, target.width()))
        target.setHeight(max(1, target.height()))
        reader.setScaledSize(target)
    image = reader.read()
    if image.isNull():
        # No Qt image plugin for it (a GIF without qgif): Pillow reads it,
        # first frame only for an animation, shrunk before converting.
        try:
            from PIL import Image
            with Image.open(asset_source(full) or full) as source:
                source.thumbnail(fit_box(*source.size, edge))
                rgba = source.convert('RGBA')
            image = QImage(rgba.tobytes(), rgba.width, rgba.height,
                           rgba.width * 4, QImage.Format_RGBA8888).copy()
        except Exception:
            return QImage()
    return shrink(image, edge)


#: normalised path -> (stamp, (width, height)): image sizes read from
#: headers, so the grid knows a tile's shape before its thumbnail.
_sizes = {}


def known_size(path):
    """The last size read for *path* (not checked against the file: the
    grid uses it at once, and a background read corrects it), or None."""
    held = _sizes.get(_key(path))
    return held[1] if held is not None else None


def read_size(path, stamp=None):
    """*path*'s ``(width, height)`` from its header (cached by stamp);
    ``(0, 0)`` when unreadable. Safe on a worker thread."""
    key = _key(path)
    if stamp is None:
        stamp = asset_stamp(path)
    held = _sizes.get(key)
    if held is not None and held[0] == stamp:
        return held[1]
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt5.QtGui import QImageReader
    full = os.fspath(path)
    size = (0, 0)
    try:
        if os.path.isfile(full):
            found = QImageReader(full).size()
        else:
            data = asset_bytes(full)
            found = None
            if data is not None:
                buffer = QBuffer()
                buffer.setData(QByteArray(data))
                buffer.open(QIODevice.ReadOnly)
                found = QImageReader(buffer).size()
        if found is not None and found.isValid():
            size = (found.width(), found.height())
        else:
            from PIL import Image
            with Image.open(asset_source(full) or full) as source:
                size = source.size
    except Exception:
        pass
    if stamp is not None:
        _sizes[key] = (stamp, size)
    return size


def image_thumbnail(path, edge=THUMBNAIL_EDGE):
    """The cached thumbnail of image asset *path* (see :func:`cached`)."""
    return cached(path, lambda: decode_image(path, edge), edge)


def clear():
    """Forget every thumbnail (tests; a project switch)."""
    _cache.clear()
    _sizes.clear()
    _held[0] = 0
    stats['decoded'] = stats['hits'] = 0
