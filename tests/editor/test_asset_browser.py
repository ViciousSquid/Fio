"""Tests for event-driven Asset Browser filesystem refreshes."""

import os
import time

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtCore import QCoreApplication, QSize  # noqa: E402
from PyQt5.QtGui import QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor.asset_browser import (  # noqa: E402
    AssetBrowser, AssetBrowserTab, AssetItem, DEFAULT_THUMBNAIL_SIZE, THUMBNAIL_SIZES,
    TILE_SPACING,
)

pytestmark = pytest.mark.qt


@pytest.fixture(scope="session")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _write_png(path):
    image = QImage(8, 8, QImage.Format_RGBA8888)
    image.fill(0xFFFFFFFF)
    assert image.save(str(path), "PNG")


def _pump_until(app, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def test_new_texture_appears_from_filesystem_event(qt_app, tmp_path):
    tab = AssetBrowserTab(
        str(tmp_path),
        [".png"],
        editor=None,
        is_model_tab=False,
    )

    assert tab.items == []

    new_asset = tmp_path / "new_texture.png"
    _write_png(new_asset)

    assert _pump_until(qt_app, lambda: len(tab.items) == 1)
    assert tab.items[0].file_path == str(new_asset)


def test_asset_browser_uses_directory_watcher_not_polling_timer(qt_app, tmp_path):
    tab = AssetBrowserTab(
        str(tmp_path),
        [".png"],
        editor=None,
        is_model_tab=False,
    )

    assert str(tmp_path) in tab._watched_asset_dirs
    assert not hasattr(tab, "asset_refresh_timer")
    assert tab.asset_watcher.directories() == [str(tmp_path)]


def test_thumbnail_rows_fit_the_viewport_including_layout_margins(qt_app, tmp_path):
    for index in range(10):
        _write_png(tmp_path / f'asset-{index}.png')
    tab = AssetBrowserTab(str(tmp_path), ['.png'], editor=None)
    tab.resize(550, 500)
    tab.show()
    try:
        view = tab.view
        assert _pump_until(qt_app, lambda: view.horizontalScrollBar().maximum() == 0)
        # Every tile lies inside the viewport's width: rows wrap, never scroll.
        width = view.viewport().width()
        for row in range(tab.model.rowCount()):
            rect = view.visualRect(tab.model.index(row))
            assert rect.left() >= 0 and rect.left() + AssetItem.tile_size(tab.thumb_size)[0] <= width
    finally:
        tab.close()
        tab.deleteLater()


def _texture_folder(tmp_path, count=3):
    folder = tmp_path / "textures"
    folder.mkdir()
    for index in range(count):
        _write_png(folder / f"tex{index}.png")
    return folder


@pytest.mark.parametrize("name, edge", [("medium", 90), ("small", 72), ("smallest", 58)])
def test_thumbnail_sizes_scale_tiles_and_columns(qt_app, tmp_path, name, edge):
    from editor.asset_browser import THUMBNAIL_SIZES, AssetItem

    assert THUMBNAIL_SIZES[name] == edge
    tab = AssetBrowserTab(str(_texture_folder(tmp_path)), [".png"])
    tab.resize(400, 300)
    tab.show()
    qt_app.processEvents()
    tab.set_thumbnail_size(edge)
    qt_app.processEvents()
    item = tab.items[0]
    tile = AssetItem.tile_size(edge)
    assert tab.delegate.sizeHint(None, tab.model.index(0)) == QSize(*tile)
    # Tiles are laid out a tile plus the spacing apart.
    first, second = (tab.view.visualRect(tab.model.index(i)) for i in (0, 1))
    assert second.left() - first.left() == tile[0] + TILE_SPACING
    pixmap = tab.thumbnail_pixmap(item)
    assert pixmap.width() <= edge and pixmap.height() <= edge
    assert max(pixmap.width(), pixmap.height()) == edge
    tab.close()


def test_right_clicking_blank_space_offers_thumbnail_sizes(qt_app, tmp_path, monkeypatch):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QMenu

    tab = AssetBrowserTab(str(_texture_folder(tmp_path)), [".png"])
    tab.resize(400, 300)
    tab.show()
    qt_app.processEvents()
    offered = []

    def choose_smallest(menu, *_args):
        offered.append([(action.text(), action.isChecked()) for action in menu.actions()])
        menu.actions()[2].trigger()

    monkeypatch.setattr(QMenu, "exec_", choose_smallest)
    viewport = tab.view.viewport()
    tab._show_grid_menu(tab.view.visualRect(tab.model.index(0)).center())
    assert offered == []  # right-clicking an asset is not blank space
    tab._show_grid_menu(QPoint(viewport.width() - 2, viewport.height() - 2))
    assert offered == [[("Medium", False), ("Small", False), ("Smallest", True)]]
    assert tab.thumb_size == 58
    tab.close()


def test_new_asset_browser_uses_smallest_thumbnails(qt_app, tmp_path):
    """Default applies to all grid tabs, not just the View menu checkmark."""
    browser = AssetBrowser(str(_texture_folder(tmp_path)))
    try:
        assert DEFAULT_THUMBNAIL_SIZE == "smallest"
        assert browser.thumbnail_size == "smallest"
        for tab in (browser.tab_textures, browser.tab_models, browser.tab_audio):
            assert tab.thumb_size == THUMBNAIL_SIZES["smallest"]
        tab = browser.tab_textures
        assert tab.delegate.sizeHint(None, tab.model.index(0)).width() == 58
    finally:
        browser.close()
        browser.deleteLater()


def test_saved_thumbnail_preference_takes_precedence(qt_app, tmp_path, main_window):
    """Changing the default must not override a previously saved choice."""
    # Use the real editor owner, as other behavioural editor tests do.
    main_window.config["AssetBrowser"] = {"thumbnail_size": "medium"}
    browser = AssetBrowser(str(_texture_folder(tmp_path)), editor=main_window)
    try:
        assert browser.thumbnail_size == "medium"
        for tab in (browser.tab_textures, browser.tab_models, browser.tab_audio):
            assert tab.thumb_size == THUMBNAIL_SIZES["medium"]
    finally:
        browser.close()
        browser.deleteLater()
