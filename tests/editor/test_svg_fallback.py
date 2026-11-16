"""The editor's SVG controls still draw on a Qt without QtSvg.

The PyQt5-WinARM64 wheel (Windows on ARM, a desktop release target) ships no
QtSvg: no ``svg`` icon engine, no ``svg`` image format. Every control icon
loaded as a null icon there -- the Windows ARM64 Qt tier caught it as dock
title buttons 3 px wide (a null icon's -1 px plus the 4 px margin) -- and the
stylesheet's check marks and arrows drew nothing. ``engine.asset_qt`` falls
back to the PNGs ``tools/render_ui_icons.py`` pre-renders.

These tests force the no-SVG path on any platform, and hold the committed
rasters to their SVGs wherever QtSvg exists.
"""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QRect, QSize      # noqa: E402
from PyQt5.QtGui import QImage             # noqa: E402
from PyQt5.QtWidgets import QStyle, QStyleOptionDockWidget  # noqa: E402

from engine import asset_qt                # noqa: E402
from engine.asset_qt import (RASTER_SIZES, load_icon, portable_stylesheet,  # noqa: E402
                             raster_fallbacks)
from tests.helpers.paths import REPO_ROOT  # noqa: E402

pytestmark = pytest.mark.qt

CONTROLS = Path(REPO_ROOT) / "assets" / "ui" / "controls"
SVGS = sorted(CONTROLS.glob("*.svg"))


@pytest.fixture
def without_svg(monkeypatch):
    """What a Qt build without QtSvg reports."""
    monkeypatch.setattr(asset_qt, "_svg_supported", False)


def test_every_control_svg_has_its_rasters():
    assert len(SVGS) > 20
    for svg in SVGS:
        found = dict(raster_fallbacks(svg))
        assert sorted(found) == list(RASTER_SIZES), (
            "%s has no PNG fallbacks; run tools/render_ui_icons.py" % svg.name)
        for size, path in found.items():
            image = QImage(path)
            assert (image.width(), image.height()) == (size, size), path
            assert image.hasAlphaChannel(), path


def test_the_rasters_still_match_their_svgs(qt_app):
    """An edited SVG whose PNGs were not re-rendered would show the old
    icon on Windows on ARM only."""
    pytest.importorskip("PyQt5.QtSvg")
    from tools.render_ui_icons import render
    for svg in SVGS:
        for size, path in raster_fallbacks(svg):
            committed = QImage(path).convertToFormat(QImage.Format_ARGB32)
            fresh = render(svg, size).convertToFormat(QImage.Format_ARGB32)
            a = committed.constBits().asstring(committed.sizeInBytes())
            b = fresh.constBits().asstring(fresh.sizeInBytes())
            mean = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
            assert mean <= 3.0, (
                "%s no longer matches %s (mean channel difference %.1f); run "
                "tools/render_ui_icons.py" % (os.path.basename(path), svg.name, mean))


def test_control_icons_load_from_their_rasters_without_svg(without_svg, qt_app):
    for path in (str(CONTROLS / "close.svg"), "assets/ui/controls/refresh.svg"):
        cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        try:
            icon = load_icon(path)
        finally:
            os.chdir(cwd)
        assert not icon.isNull(), path
        assert icon.actualSize(QSize(16, 16)) == QSize(16, 16)
        assert icon.actualSize(QSize(48, 48)) == QSize(48, 48)
        assert not icon.pixmap(16, 16).isNull()


def test_dock_title_buttons_keep_their_size_without_svg(without_svg, qt_app):
    """The ARM64 failure itself: the style's title-bar icons are rasters,
    so the close and float buttons are 16 px icons plus the 4 px margin."""
    style = qt_app.style()
    option = QStyleOptionDockWidget()
    option.rect = QRect(0, 0, 400, 28)
    option.closable = option.floatable = True
    for element in (QStyle.SE_DockWidgetCloseButton, QStyle.SE_DockWidgetFloatButton):
        rect = style.subElementRect(element, option, None)
        assert (rect.width(), rect.height()) == (20, 20)
    for icon in (QStyle.SP_TitleBarCloseButton, QStyle.SP_MessageBoxWarning,
                 QStyle.SP_DirIcon, QStyle.SP_FileIcon):
        assert not style.standardIcon(icon).isNull()


def test_stylesheet_images_swap_to_rasters_without_svg(without_svg):
    from editor_styles import CONTROL_STYLESHEET
    from main import dark_stylesheet
    for sheet in (CONTROL_STYLESHEET, dark_stylesheet):
        portable = portable_stylesheet(sheet)
        assert ".svg" not in portable
        for url in re.findall(r"url\(([^)]+)\)", portable):
            url = url.strip("'\"")
            if url.startswith("data:"):
                continue
            assert (Path(REPO_ROOT) / url).is_file(), url


def test_stylesheets_are_untouched_where_qt_draws_svg(monkeypatch):
    from editor_styles import CONTROL_STYLESHEET
    monkeypatch.setattr(asset_qt, "_svg_supported", True)
    assert portable_stylesheet(CONTROL_STYLESHEET) is CONTROL_STYLESHEET
