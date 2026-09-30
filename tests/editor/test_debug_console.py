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
    assert 'href="filter:2"' not in html
    assert 'href="filter:5"' not in html
    assert 'href="filter:8"' not in html
    assert "#F08000" in html or "#f08000" in html
    assert "#FFFFFF" in html or "#ffffff" in html
    assert "Click to filter by" not in html
    assert "filter:version" not in html

    # The first three numeric components are orange; the periods and build
    # component remain white, and the version components are presentation-only.
    assert '<span style="color:#f08000;">2</span>' in html or 'color:#f08000;">2</span>' in html
    assert '<span style="color:#f08000;">5</span>' in html or 'color:#f08000;">5</span>' in html
    assert '<span style="color:#f08000;">8</span>' in html or 'color:#f08000;">8</span>' in html

    console.deleteLater()


def test_startup_banner_source_matches_requested_shape():
    source = Path("editor/io_handlers.py").read_text(encoding="utf-8")

    assert "Fio version {version_str}" in source
    assert 'debug_log_raw("github.com/vicioussquid/Fio")' in source
    assert "Registered {len(io_manager._input_handlers)} input handlers" in source
    assert "Type 'help' to see all available commands" in source


def test_a_burst_of_messages_is_inserted_in_batches(qt_app):
    """A monster fight with I/O logging on logs hundreds of lines a second
    from worker threads; inserting each as it arrived froze the editor."""
    console = DebugConsole()
    console.clear()
    inserts = []
    real_insert = console._insert_lines

    def counting(lines):
        inserts.append(len(lines))
        real_insert(lines)

    console._insert_lines = counting
    for i in range(1000):
        console._on_message("IO", "[IO] Relay_%d.OnTrigger -> Relay_%d.Trigger" % (i, i + 1))
    # The first line is immediate; the rest wait for one flush.
    assert inserts == [1]
    console._flush_timer.stop()
    console._flush_pending()
    # One insert for the rest, capped, with a note of what was skipped.
    assert len(inserts) == 2
    assert inserts[1] == console.MAX_LINES_PER_FLUSH + 1
    text = console.console.toPlainText()
    assert "Relay_999.OnTrigger" in text
    assert "messages not shown" in text
    console.deleteLater()


def test_an_isolated_message_still_appears_at_once(qt_app):
    console = DebugConsole()
    console.clear()
    console._flush_timer.stop()
    console._on_message("Info", "[Info] hello there")
    assert "hello there" in console.console.toPlainText()
    console.deleteLater()


def test_the_document_is_rebuilt_before_it_grows_without_bound(qt_app):
    console = DebugConsole()
    console.clear()
    for i in range(console.MAX_DOCUMENT_LINES + 10):
        console._insert_lines(['line %d<br>' % i])
    assert console._document_lines <= console.MAX_DOCUMENT_LINES
    console.deleteLater()
