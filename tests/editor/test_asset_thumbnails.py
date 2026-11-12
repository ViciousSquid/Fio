"""Asset Browser tiles hold small cached thumbnails, never full-size images.

Each asset is decoded once per version of its file, at no more than the
largest tile's edge, and shared by every tile showing it; an edited file is
decoded afresh. (editor/asset_thumbnails.py)
"""

import os

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor import asset_thumbnails  # noqa: E402
from editor.asset_browser import AssetBrowserTab, AssetItem, THUMBNAIL_SIZES  # noqa: E402
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


def test_a_large_texture_is_held_downscaled_and_decoded_once(qt_app, tmp_path):
    path = tmp_path / "big.png"
    _write(path, 2048, 1024, "#c03020")
    first = AssetItem("big.png", str(path), None)
    image = first._source_image
    # Fitted inside the largest tile, aspect kept: not 8 MB of pixels.
    edge = asset_thumbnails.THUMBNAIL_EDGE
    assert (image.width(), image.height()) == (edge, edge // 2)
    assert not hasattr(first, '_source_pixmap')
    pixmap = first.thumb_label.pixmap()
    assert max(pixmap.width(), pixmap.height()) == first.thumb_size
    assert asset_thumbnails.stats['decoded'] == 1
    # Another tile of the same file -- a folder revisited -- shares it.
    second = AssetItem("big.png", str(path), None)
    assert asset_thumbnails.stats['decoded'] == 1
    assert second._source_image.cacheKey() == image.cacheKey()
    # A size change rescales the small image; nothing is decoded.
    second.set_thumbnail_size(THUMBNAIL_SIZES['medium'])
    assert max(second.thumb_label.pixmap().width(),
               second.thumb_label.pixmap().height()) == THUMBNAIL_SIZES['medium']
    assert asset_thumbnails.stats['decoded'] == 1


def test_an_edited_file_is_decoded_again(qt_app, tmp_path):
    path = tmp_path / "tex.png"
    _write(path, 256, 256, "#ff0000")
    red = AssetItem("tex.png", str(path), None)._source_image
    assert QColor(red.pixel(10, 10)).red() > 200
    before = asset_stamp(str(path))
    _write(path, 256, 256, "#0000ff")
    _bump(path, 2)                      # a coarse filesystem clock
    assert asset_stamp(str(path)) != before
    blue = AssetItem("tex.png", str(path), None)._source_image
    assert QColor(blue.pixel(10, 10)).blue() > 200
    assert asset_thumbnails.stats['decoded'] == 2
    assert len(asset_thumbnails._cache) == 1        # replaced, not added


def test_a_folder_visited_again_decodes_nothing(qt_app, tmp_path):
    for i in range(5):
        _write(tmp_path / f"t{i}.png", 512, 512, "#808080")
    tab = AssetBrowserTab(str(tmp_path), [".png"], editor=None, is_model_tab=False)
    assert len(tab.items) == 5
    assert asset_thumbnails.stats['decoded'] == 5
    tab.load_directory(str(tmp_path))
    assert len(tab.items) == 5
    assert asset_thumbnails.stats['decoded'] == 5
    for item in tab.items:
        assert max(item._source_image.width(), item._source_image.height()) \
            <= asset_thumbnails.THUMBNAIL_EDGE


def test_an_unreadable_file_shows_a_question_mark_and_is_not_retried(qt_app, tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not a png")
    item = AssetItem("broken.png", str(path), None)
    assert item._source_image is None and item.thumb_label.text() == "?"
    AssetItem("broken.png", str(path), None)
    assert asset_thumbnails.stats['decoded'] == 1


def test_the_cache_is_bounded(qt_app, tmp_path, monkeypatch):
    monkeypatch.setattr(asset_thumbnails, 'CACHE_LIMIT', 3)
    for i in range(5):
        path = tmp_path / f"c{i}.png"
        _write(path, 64, 64, "#00ff00")
        asset_thumbnails.image_thumbnail(str(path))
    assert len(asset_thumbnails._cache) == 3
    # The least recently used went first.
    kept = {os.path.basename(k) for k in asset_thumbnails._cache}
    assert kept == {"c2.png", "c3.png", "c4.png"}


def test_a_missing_asset_has_no_stamp_and_is_not_cached(qt_app, tmp_path):
    missing = tmp_path / "gone.png"
    assert asset_stamp(str(missing)) is None
    assert asset_thumbnails.image_thumbnail(str(missing)).isNull()
    assert not asset_thumbnails._cache
