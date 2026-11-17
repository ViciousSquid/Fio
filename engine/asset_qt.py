"""Qt images, icons and fonts from assets, loose or inside a .fiopak.

Qt's own constructors (``QPixmap(path)``, ``QIcon(path)``,
``QFontDatabase.addApplicationFont(path)``) read only the file system. These
take the same path and fall back to the archive member
(:mod:`engine.asset_archives`), so an archive-only installation needs no
extracted copies. A loose file is still opened by path, exactly as before.
"""

from __future__ import annotations

import os

from engine.asset_archives import asset_bytes


def _data(path):
    """Bytes of the archived asset at *path*, or None (loose or absent)."""
    if path is None or os.path.isfile(os.fspath(path)):
        return None
    return asset_bytes(path)


def load_image(path):
    """A ``QImage`` of the asset at *path* (null when there is none)."""
    from PyQt5.QtGui import QImage
    if not isinstance(path, (str, os.PathLike, type(None))):
        return QImage(path)
    data = _data(path)
    if data is None:
        return QImage(os.fspath(path)) if path is not None else QImage()
    image = QImage()
    image.loadFromData(data)
    return image


def load_pixmap(path):
    """A ``QPixmap`` of the asset at *path* (null when there is none); any
    other argument -- a QImage, another pixmap -- is handed to ``QPixmap``."""
    from PyQt5.QtGui import QPixmap
    if not isinstance(path, (str, os.PathLike, type(None))):
        return QPixmap(path)
    data = _data(path)
    if data is None:
        return QPixmap(os.fspath(path)) if path is not None else QPixmap()
    pixmap = QPixmap()
    pixmap.loadFromData(data)
    return pixmap


#: Sizes ``tools/render_ui_icons.py`` pre-renders each control SVG at.
RASTER_SIZES = (16, 32, 64)

_svg_supported = None


def svg_supported():
    """Whether this Qt can draw SVG images and icons.

    QtSvg provides both the ``svg`` image format and the ``svg`` icon engine;
    builds without it (the PyQt5-WinARM64 wheel) load every SVG as a null
    icon and draw nothing for an SVG stylesheet image.
    """
    global _svg_supported
    if _svg_supported is None:
        from PyQt5.QtGui import QImageReader
        formats = {bytes(f).lower() for f in QImageReader.supportedImageFormats()}
        _svg_supported = b"svg" in formats
    return _svg_supported


def raster_fallbacks(path):
    """``[(size, path)]`` of the PNGs pre-rendered for the SVG at *path*.

    ``assets/ui/controls/close.svg`` has ``assets/ui/controls/raster/close-16.png``
    and so on (see ``tools/render_ui_icons.py``). Only those that exist,
    loose or in an archive, are listed.
    """
    text = os.fspath(path)
    if not text.lower().endswith(".svg"):
        return []
    folder, name = os.path.split(text)
    stem = name[:-4]
    found = []
    for size in RASTER_SIZES:
        candidate = os.path.join(folder, "raster", "%s-%d.png" % (stem, size))
        if os.path.isfile(candidate) or asset_bytes(candidate) is not None:
            found.append((size, candidate))
    return found


def portable_stylesheet(stylesheet):
    """*stylesheet* with each SVG ``url(...)`` swapped for its PNG fallback
    when this Qt cannot draw SVG; unchanged otherwise.

    Every SVG image in Fio's stylesheets sets its own width and height, and a
    stylesheet image is drawn as an icon, so the 32 px raster scales down to
    the 10-16 px boxes and stays sharp at a 2x device ratio.
    """
    if svg_supported():
        return stylesheet
    import re

    def swap(match):
        rasters = dict(raster_fallbacks(match.group(2)))
        replacement = rasters.get(32) or (next(iter(rasters.values())) if rasters else None)
        if replacement is None:
            return match.group(0)
        return "url(%s%s%s)" % (match.group(1), replacement.replace(os.sep, "/"), match.group(1))

    return re.sub(r"""url\((["']?)([^"')]+?\.svg)\1\)""", swap, stylesheet)


def load_icon(path):
    """A ``QIcon`` of the asset at *path* (null when there is none); any
    other argument -- a QPixmap, another icon -- is handed to ``QIcon``.

    An SVG on a Qt that cannot draw SVG loads from its pre-rendered PNGs
    (:func:`raster_fallbacks`) instead of loading as a null icon.
    """
    from PyQt5.QtGui import QIcon
    if not isinstance(path, (str, os.PathLike, type(None))):
        return QIcon(path)
    if path is not None and not svg_supported():
        rasters = raster_fallbacks(path)
        if rasters:
            icon = QIcon()
            for _size, raster in rasters:
                icon.addPixmap(load_pixmap(raster))
            return icon
    data = _data(path)
    if data is None:
        return QIcon(os.fspath(path)) if path is not None else QIcon()
    return QIcon(load_pixmap(path))


def image_size(path):
    """The ``QSize`` of the image asset at *path*, read from its header
    (invalid when there is none)."""
    from PyQt5.QtGui import QImageReader
    data = _data(path)
    if data is None:
        return QImageReader(os.fspath(path)).size() if path is not None else QImageReader().size()
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.ReadOnly)
    return QImageReader(buffer).size()


def add_application_font(path) -> int:
    """``QFontDatabase.addApplicationFont`` for the font asset at *path*:
    the font id, or -1 when it cannot be loaded."""
    from PyQt5.QtGui import QFontDatabase
    data = _data(path)
    if data is None:
        return QFontDatabase.addApplicationFont(os.fspath(path))
    from PyQt5.QtCore import QByteArray
    return QFontDatabase.addApplicationFontFromData(QByteArray(data))


def shrink_image(image, factor: int):
    """*image* (a ``QImage`` or ``QPixmap``) shrunk by the whole *factor*:
    each pixel of the result is the mean (to within rounding) of a *factor*
    x *factor* block (a
    last row or column of pixels too few to fill a block is dropped). An
    even box blur, as soft as ``Qt.SmoothTransformation``'s reduction.

    Worked out by Pillow's box reduction (C, on the calling thread), never
    Qt's scaler: Qt 5.15 splits a large smooth scale -- and a large format
    conversion -- across ``QThreadPool.globalInstance()`` and blocks the
    caller until every part is done, and on Windows ARM64 that wait
    sometimes never ends (a whole window's picture, three times in one day
    of CI: the loading overlay raised for Play and for ``map``). A window's
    or a frame's picture is already 32 bits a pixel, so it is read in place,
    not converted; every channel is averaged alike, whatever their order
    and whether alpha is premultiplied. Returns a ``QImage`` in the same
    format (or a null one for a null image).
    """
    from PIL import Image
    from PyQt5.QtGui import QImage, QPixmap

    if isinstance(image, QPixmap):
        image = image.toImage()
    if image is None or image.isNull():
        return QImage()
    if image.depth() != 32:
        image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    f = max(1, int(factor))
    w, h = image.width(), image.height()
    out_w, out_h = max(1, w // f), max(1, h // f)
    fx, fy = min(f, w), min(f, h)                 # a picture smaller than a block
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    # Four independent 8-bit channels: Pillow premultiplies an 'RGBA' image
    # by its alpha before reducing it (lossy, and wrong for Qt's premultiplied
    # pixels); 'CMYK' is four bands it averages as they are.
    picture = Image.frombuffer('CMYK', (w, h), memoryview(bits), 'raw', 'CMYK',
                               image.bytesPerLine(), 1)
    small = picture.reduce((fx, fy), box=(0, 0, out_w * fx, out_h * fy))
    data = small.tobytes()
    return QImage(data, out_w, out_h, out_w * 4, image.format()).copy()
