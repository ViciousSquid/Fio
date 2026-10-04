"""Tests for event-driven Asset Browser filesystem refreshes."""

import os
import time

import pytest

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtGui import QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor.asset_browser import AssetBrowserTab  # noqa: E402

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

    # The existing resize timer is unrelated UI debouncing; it must not be
    # responsible for discovering filesystem changes.
    assert tab.resize_timer.isSingleShot()
