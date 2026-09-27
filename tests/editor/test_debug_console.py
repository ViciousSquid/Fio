"""Regression tests for the Debug Console startup banner/link presentation."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor.debug_console import DebugConsole, debug_log_raw  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(scope="session")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_raw_console_line_has_no_category_prefix(qt_app):
    console = DebugConsole()
    console.clear()

    debug_log_raw("github.com/vicioussquid/Fio")

    assert "github.com/vicioussquid/Fio" in console.console.toPlainText()
    assert "[Info] github.com/vicioussquid/Fio" not in console.console.toPlainText()

    console.deleteLater()


def test_github_startup_link_is_dark_green_and_clickable(qt_app):
    console = DebugConsole()
    console.clear()

    console._append_message("", "github.com/vicioussquid/Fio")
    html = console.console.toHtml()

    assert "https://github.com/vicioussquid/Fio" in html
    assert "#2b6132" in html

    console.deleteLater()


def test_version_banner_is_rendered_once(qt_app):
    console = DebugConsole()
    console.clear()

    console._append_message("Info", "[Info] Fio version 2.5.8.2709")
    html = console.console.toHtml()

    assert "Fio version" in html
    assert 'href="filter:2"' in html
    assert 'href="filter:5"' in html
    assert 'href="filter:8"' in html
    assert "#66BB6A" in html or "#66bb6a" in html
    assert "#F08000" in html or "#f08000" in html
    assert "Click to filter by" not in html
    assert "filter:version" not in html

    console.deleteLater()


def test_startup_banner_source_matches_requested_shape():
    source = Path("editor/io_handlers.py").read_text(encoding="utf-8")

    assert "Fio version {version_str}" in source
    assert 'debug_log_raw("github.com/vicioussquid/Fio")' in source
    assert "Registered {len(io_manager._input_handlers)} input handlers" in source
    assert "Type 'help' to see all available commands" in source
