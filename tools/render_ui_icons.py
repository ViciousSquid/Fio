#!/usr/bin/env python3
"""Render the editor's SVG controls to PNG fallbacks.

Qt draws ``assets/ui/controls/*.svg`` through its SVG plugins (the ``svg``
icon engine and image format). Not every Qt build ships them: the
PyQt5-WinARM64 wheel Fio installs on Windows on ARM has no QtSvg at all,
so every SVG icon -- dock buttons, message-box icons, file-dialog icons,
the stylesheet's check marks and arrows -- loads as a null icon there.
:func:`engine.asset_qt.load_icon` and :func:`engine.asset_qt.portable_stylesheet`
fall back to the PNGs this writes to ``assets/ui/controls/raster/``.

Run it after adding or editing a control SVG (on a Qt with SVG support):

    QT_QPA_PLATFORM=offscreen python tools/render_ui_icons.py

``tests/editor/test_svg_fallback.py`` fails when a raster is missing or no
longer matches its SVG.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ROOT / "assets" / "ui" / "controls"
RASTER = CONTROLS / "raster"
SIZES = (16, 32, 64)


def raster_path(svg: Path, size: int) -> Path:
    return RASTER / ("%s-%d.png" % (svg.stem, size))


def render(svg: Path, size: int):
    """The SVG drawn into a transparent ``size`` x ``size`` QImage."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage, QPainter
    from PyQt5.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg))
    if not renderer.isValid():
        raise ValueError("not a renderable SVG: %s" % svg)
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer.render(painter)
    painter.end()
    return image


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    RASTER.mkdir(exist_ok=True)
    svgs = sorted(CONTROLS.glob("*.svg"))
    for svg in svgs:
        for size in SIZES:
            target = raster_path(svg, size)
            if not render(svg, size).save(str(target), "PNG"):
                print("could not write %s" % target, file=sys.stderr)
                return 1
    print("rendered %d SVGs at %s px into %s"
          % (len(svgs), ", ".join(map(str, SIZES)), RASTER.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
