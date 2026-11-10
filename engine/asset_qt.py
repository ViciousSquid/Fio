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


def load_icon(path):
    """A ``QIcon`` of the asset at *path* (null when there is none); any
    other argument -- a QPixmap, another icon -- is handed to ``QIcon``."""
    from PyQt5.QtGui import QIcon
    if not isinstance(path, (str, os.PathLike, type(None))):
        return QIcon(path)
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
