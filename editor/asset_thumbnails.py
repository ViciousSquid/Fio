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


def shrink(image, edge=THUMBNAIL_EDGE):
    """*image* fitted inside *edge* x *edge* (aspect kept); as it is when it
    already fits or is null."""
    from PyQt5.QtCore import Qt
    if image.isNull() or (image.width() <= edge and image.height() <= edge):
        return image
    return image.scaled(edge, edge, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def cached(path, make, edge=THUMBNAIL_EDGE):
    """The thumbnail ``QImage`` of the asset at *path*: the cached one while
    the asset is unchanged, else ``make()`` (a QImage, a QPixmap or None),
    shrunk to *edge* and kept. A null image is kept too: an unreadable
    file is not tried again until it changes. An asset with no stamp (it
    exists nowhere) is made each time and not kept."""
    from PyQt5.QtGui import QImage, QPixmap
    key = _key(path)
    stamp = asset_stamp(path)
    held = _cache.get(key)
    if held is not None and stamp is not None and held[0] == (stamp, edge):
        _cache.move_to_end(key)
        stats['hits'] += 1
        return held[1]
    made = make()
    stats['decoded'] += 1
    if isinstance(made, QPixmap):
        made = made.toImage()
    image = shrink(made, edge) if isinstance(made, QImage) else QImage()
    _forget(key)
    if stamp is not None:
        _cache[key] = ((stamp, edge), image)
        _held[0] += image.sizeInBytes()
        while _held[0] > CACHE_BYTES and len(_cache) > 1:
            _forget(next(iter(_cache)))
    return image


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
    if size.isValid() and (size.width() > edge or size.height() > edge):
        target = size.scaled(edge, edge, Qt.KeepAspectRatio)
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
                source.thumbnail((edge, edge))
                rgba = source.convert('RGBA')
            image = QImage(rgba.tobytes(), rgba.width, rgba.height,
                           rgba.width * 4, QImage.Format_RGBA8888).copy()
        except Exception:
            return QImage()
    return shrink(image, edge)


def image_thumbnail(path, edge=THUMBNAIL_EDGE):
    """The cached thumbnail of image asset *path* (see :func:`cached`)."""
    return cached(path, lambda: decode_image(path, edge), edge)


def clear():
    """Forget every thumbnail (tests; a project switch)."""
    _cache.clear()
    _held[0] = 0
    stats['decoded'] = stats['hits'] = 0
