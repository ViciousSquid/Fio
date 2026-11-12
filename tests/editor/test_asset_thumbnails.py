"""Asset Browser thumbnails: small, cached, decoded off the UI thread, and
only for the tiles on screen.

Each asset is decoded once per version of its file, at no more than the
largest tile's edge, keeping its shape; an edited file is decoded afresh.
A folder of thousands opens at once. (editor/asset_thumbnails.py,
editor/asset_browser.py)
"""

import os
import time

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor import asset_thumbnails  # noqa: E402
from editor.asset_browser import (  # noqa: E402
    TILE_SPACING, AssetBrowserTab, THUMBNAIL_SIZES, _thumb_rect)
from engine.asset_archives import asset_stamp  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fresh_cache():
    asset_thumbnails.clear()
    yield
    asset_thumbnails.clear()


def _write(path, width, height, colour):
    image = QImage(width, height, QImage.Format_RGBA8888)
    image.fill(QColor(colour))
    assert image.save(str(path))


def _bump(path, seconds):
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + int(seconds * 1e9)))


def _tab(qt_app, folder, size=(500, 300)):
    tab = AssetBrowserTab(str(folder), [".png"], editor=None, is_model_tab=False)
    tab.resize(*size)
    tab.show()
    qt_app.processEvents()
    tab.finish_thumbnails()
    return tab


def _revisit(qt_app, tab, folder):
    tab.load_directory(str(folder))
    qt_app.processEvents()
    tab.finish_thumbnails()


def test_a_large_texture_is_held_downscaled_and_decoded_once(qt_app, tmp_path):
    path = tmp_path / "big.png"
    _write(path, 2048, 1024, "#c03020")
    tab = _tab(qt_app, tmp_path)
    try:
        image = asset_thumbnails.lookup(str(path))
        # Fitted inside two of the largest tile's slots (it is wide), aspect
        # kept: 180x90, not 8 MB of pixels.
        edge = asset_thumbnails.THUMBNAIL_EDGE
        assert (image.width(), image.height()) == (2 * edge, edge)
        pixmap = tab.thumbnail_pixmap(tab.items[0])
        assert (pixmap.width(), pixmap.height()) == (2 * tab.thumb_size, tab.thumb_size)
        assert asset_thumbnails.stats['decoded'] == 1
        # The folder again -- a revisit -- decodes nothing.
        _revisit(qt_app, tab, tmp_path)
        assert tab.thumbnail_pixmap(tab.items[0]) is not None
        assert asset_thumbnails.stats['decoded'] == 1
        # A size change rescales the small image; nothing is decoded.
        tab.set_thumbnail_size(THUMBNAIL_SIZES['medium'])
        pixmap = tab.thumbnail_pixmap(tab.items[0])
        assert pixmap.height() == THUMBNAIL_SIZES['medium']
        assert asset_thumbnails.stats['decoded'] == 1
    finally:
        tab.close()


@pytest.mark.parametrize("size, span, shape", [
    ((512, 256), (2, 1), (116, 58)),     # twice as wide: two slots across
    ((256, 512), (1, 2), (58, 116)),     # twice as tall: two slots down
    ((1024, 256), (2, 1), (120, 30)),    # wider still: two slots, fitted in them
    ((400, 300), (1, 1), (58, 43)),      # 4:3 -- one slot
    ((300, 300), (1, 1), (58, 58)),
])
def test_a_tile_takes_slots_by_its_textures_shape(qt_app, tmp_path, size, span, shape):
    """A texture twice as wide as it is tall takes two slots across, a tall
    one two down -- Metro's wide and tall tiles; the picture fills the tile
    in its own shape, centred, never stretched."""
    _write(tmp_path / "tex.png", *size, "#ff0000")
    tab = _tab(qt_app, tmp_path)
    try:
        tab.set_thumbnail_size(58)
        item = tab.items[0]
        assert item.span == span
        cell = tab.view.visualRect(tab.model.index(0))
        slot_w, slot_h = tab.view.slot()
        # Metro tiles: a slot is a tile and the gutter; two slots cover it.
        assert (cell.width(), cell.height()) == (span[0] * slot_w - TILE_SPACING,
                                                 span[1] * slot_h - TILE_SPACING)
        pixmap = tab.thumbnail_pixmap(item)
        assert (pixmap.width(), pixmap.height()) == shape
        # On screen: red where the picture is, the tile's grey around it.
        qt_app.processEvents()
        grab = tab.view.viewport().grab().toImage()
        thumb = _thumb_rect(cell)
        centre = thumb.center()
        assert QColor(grab.pixel(centre)).red() > 200
        if shape[0] < thumb.width() - 4:
            assert QColor(grab.pixel(thumb.left() + 1, centre.y())).red() < 100
        if shape[1] < thumb.height() - 4:
            assert QColor(grab.pixel(centre.x(), thumb.top() + 1)).red() < 100
    finally:
        tab.close()


def test_slots_are_packed_in_order_without_holes():
    from editor.asset_browser import pack_tiles
    # 3 columns: the wide tile does not fit beside two squares, so it starts
    # row 1 -- and the square after it fills the gap it left in row 0; the
    # tall one takes the slot beside it, two rows down.
    places, rows = pack_tiles([(1, 1), (1, 1), (2, 1), (1, 1), (1, 2), (1, 1)], 3)
    assert places == [(0, 0), (1, 0), (0, 1), (2, 0), (2, 1), (0, 2)]
    assert rows == 3
    # Wider than the grid: a wide tile in one column takes the one slot.
    assert pack_tiles([(2, 1), (1, 2)], 1) == ([(0, 0), (0, 1)], 3)


def test_packing_many_wide_tiles_in_odd_columns_stays_fast():
    from editor.asset_browser import pack_tiles
    started = time.perf_counter()
    places, rows = pack_tiles([(2, 1)] * 20000, 5)       # every row leaves a hole
    assert rows == 10000 and len(places) == 20000
    assert time.perf_counter() - started < 2.0


def test_an_edited_file_is_decoded_again(qt_app, tmp_path):
    path = tmp_path / "tex.png"
    _write(path, 256, 256, "#ff0000")
    tab = _tab(qt_app, tmp_path)
    try:
        red = tab.thumbnail_pixmap(tab.items[0]).toImage()
        assert QColor(red.pixel(10, 10)).red() > 200
        before = asset_stamp(str(path))
        _write(path, 256, 256, "#0000ff")
        _bump(path, 2)                      # a coarse filesystem clock
        assert asset_stamp(str(path)) != before
        _revisit(qt_app, tab, tmp_path)
        blue = tab.thumbnail_pixmap(tab.items[0]).toImage()
        assert QColor(blue.pixel(10, 10)).blue() > 200
        # Decoded again (the folder watcher may also have reloaded it after
        # the write, before the stamp was bumped: a second real change).
        assert asset_thumbnails.stats['decoded'] >= 2
        assert len(asset_thumbnails._cache) == 1        # replaced, not added
        assert asset_thumbnails.held_bytes() == asset_thumbnails.lookup(str(path)).sizeInBytes()
    finally:
        tab.close()


def test_a_huge_folder_opens_at_once_and_decodes_only_what_is_shown(qt_app, tmp_path):
    _write(tmp_path / "base.png", 128, 128, "#808080")
    data = (tmp_path / "base.png").read_bytes()
    (tmp_path / "base.png").unlink()
    for i in range(5000):
        (tmp_path / f"t{i:05}.png").write_bytes(data)
    started = time.perf_counter()
    tab = _tab(qt_app, tmp_path)
    try:
        assert len(tab.items) == 5000
        assert time.perf_counter() - started < 5.0      # ~0.1 s; generous for CI
        shown = asset_thumbnails.stats['decoded']
        assert 0 < shown < 200                          # the tiles on screen
        bar = tab.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        qt_app.processEvents()
        tab.finish_thumbnails()
        assert shown < asset_thumbnails.stats['decoded'] < 2 * shown + 50
        assert tab.thumbnail_pixmap(tab.items[-1]) is not None
    finally:
        tab.close()


def test_thumbnails_are_decoded_off_the_ui_thread(qt_app, tmp_path, monkeypatch):
    import threading
    on_ui_thread = []
    original = asset_thumbnails.decode_image

    def recording(path, *args, **kwargs):
        on_ui_thread.append(threading.current_thread() is threading.main_thread())
        return original(path, *args, **kwargs)

    monkeypatch.setattr(asset_thumbnails, 'decode_image', recording)
    for i in range(3):
        _write(tmp_path / f"t{i}.png", 64, 64, "#00ff00")
    tab = _tab(qt_app, tmp_path)
    try:
        assert len(on_ui_thread) == 3 and not any(on_ui_thread)
    finally:
        tab.close()


@pytest.mark.parametrize('kind', ['decode', 'sizes'])
def test_thumbnail_workers_can_finish_after_their_tab_is_deleted(
        qt_app, tmp_path, monkeypatch, kind):
    import threading
    from PyQt5 import sip
    from PyQt5.QtCore import QThreadPool
    from PyQt5.QtWidgets import QWidget
    from editor.asset_browser import AssetItem, ThumbnailLoader

    path = tmp_path / 'late.png'
    _write(path, 64, 32, '#00ff00')
    entered, resume = threading.Event(), threading.Event()
    operation = 'decode_image' if kind == 'decode' else 'read_size'
    original = getattr(asset_thumbnails, operation)

    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(5), 'test did not release the thumbnail worker'
        return original(*args, **kwargs)

    monkeypatch.setattr(asset_thumbnails, operation, paused)
    pool = QThreadPool()
    monkeypatch.setattr(ThumbnailLoader, '_pool', pool)
    tab = QWidget()
    loader = ThumbnailLoader(tab)
    signals = loader._signals
    completed = []
    if kind == 'decode':
        signals.ready.connect(lambda *args: completed.append(args))
        loader.pixmap(AssetItem(path.name, str(path)), (58, 58))
    else:
        signals.sizes.connect(lambda *args: completed.append(args))
        loader.read_sizes([str(path)])
    try:
        assert entered.wait(5), 'thumbnail worker did not start'
        sip.delete(tab)
        assert sip.isdeleted(loader)
    finally:
        resume.set()
        assert pool.waitForDone(5000), 'thumbnail worker did not finish'
        qt_app.processEvents()
        if not sip.isdeleted(tab):
            sip.delete(tab)
    # A real worker emits after deletion, rather than checking a deletion
    # flag that can race with the emit. Qt drops delivery to the dead loader.
    assert completed
    if kind == 'decode':
        assert completed[0][2].size().width() == 64
    else:
        assert completed == [(1, [(str(path), (64, 32))]), (1, None)]


def test_an_unreadable_file_shows_a_question_mark_and_is_not_retried(qt_app, tmp_path):
    (tmp_path / "broken.png").write_bytes(b"not a png")
    tab = _tab(qt_app, tmp_path)
    try:
        pixmap = tab.thumbnail_pixmap(tab.items[0])
        assert pixmap is not None and pixmap.isNull()    # drawn as "?"
        _revisit(qt_app, tab, tmp_path)
        assert asset_thumbnails.stats['decoded'] == 1
    finally:
        tab.close()


def test_the_cache_is_bounded_by_bytes(qt_app, tmp_path, monkeypatch):
    assert asset_thumbnails.CACHE_BYTES == 100 * 1024 * 1024
    one = 64 * 64 * 4
    monkeypatch.setattr(asset_thumbnails, 'CACHE_BYTES', 3 * one)
    for i in range(5):
        path = tmp_path / f"c{i}.png"
        _write(path, 64, 64, "#00ff00")
        asset_thumbnails.image_thumbnail(str(path))
    assert len(asset_thumbnails._cache) == 3
    assert asset_thumbnails.held_bytes() == 3 * one
    # The least recently used went first.
    kept = {os.path.basename(k) for k in asset_thumbnails._cache}
    assert kept == {"c2.png", "c3.png", "c4.png"}
    # Pushed out is not lost: it is decoded again, small, when asked for.
    decoded = asset_thumbnails.stats['decoded']
    assert not asset_thumbnails.image_thumbnail(str(tmp_path / "c0.png")).isNull()
    assert asset_thumbnails.stats['decoded'] == decoded + 1
    assert asset_thumbnails.held_bytes() == 3 * one


def test_a_missing_asset_has_no_stamp_and_is_not_cached(qt_app, tmp_path):
    missing = tmp_path / "gone.png"
    assert asset_stamp(str(missing)) is None
    assert asset_thumbnails.image_thumbnail(str(missing)).isNull()
    assert not asset_thumbnails._cache
