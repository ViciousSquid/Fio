"""Qt image/icon readers for loose and category-archive assets.

Qt's filename constructors cannot read ZIP members. Decode compressed data
into Qt directly, never writing members out to an assets directory.
"""

from __future__ import annotations

from PyQt5.QtGui import QIcon, QImage, QPixmap

from engine.asset_archives import asset_bytes


def asset_image(path):
    data = asset_bytes(path)
    return QImage.fromData(data) if data is not None else QImage()


def asset_pixmap(path):
    pixmap = QPixmap()
    data = asset_bytes(path)
    if data is not None:
        pixmap.loadFromData(data)
    return pixmap


def asset_icon(path):
    return QIcon(asset_pixmap(path))
