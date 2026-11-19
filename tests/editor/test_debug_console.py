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
    assert "#26a69a" in html or "#26A69A" in html

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


def test_the_startup_opengl_line_has_no_filter_links(qt_app):
    """Its version numbers ("4.1", "90.5", "@1.5x") are hardware text, not
    entity names. Only this line: numbers elsewhere still become filters."""
    console = DebugConsole()
    console.clear()

    console._append_message(
        "Info",
        "[Info] OpenGL 4.1 Metal - 90.5 | Apple M5 | Apple | 3D view 573x506 @1.5x")
    html = console.console.toHtml()
    assert "OpenGL 4.1 Metal - 90.5 | Apple M5 | Apple | 3D view 573x506 @1.5x" in html
    assert "filter:" not in html
    assert "Click to filter by" not in html

    console._append_message("Info", "[IO] Door1.Open fired")
    assert 'href="filter:Door1"' in console.console.toHtml()

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


def test_a_toast_in_the_console_is_a_coloured_box_with_no_links(qt_app):
    """Toasts written to the console are cards -- a tinted body and an accent
    bar -- in the level's colour (green for a save, red for an error), a block each,
    an error with a copy button, and no click-to-filter links in their words -- the
    same words in an ordinary line still link."""
    from PyQt5.QtTest import QTest
    from editor.debug_console import debug_log_toast, get_debug_logger, console_plain_text
    console = DebugConsole()
    console.clear()

    debug_log_toast("Saved level_2.json: 105 brushes, Door1", 'success')
    debug_log_toast("Could not write maps/room 12.json", 'error')
    QTest.qWait(200)                       # the console draws lines in batches
    html = console.console.toHtml().lower()
    text = console.console.toPlainText().replace('\xa0', ' ')
    assert "saved level_2.json: 105 brushes, door1" in text.lower()
    assert "could not write maps/room 12.json" in text.lower()
    assert html.count("<table") == 2                     # a card each
    assert 'bgcolor="#14231a"' in html and 'bgcolor="#2b1613"' in html   # tinted
    assert "#4caf50" in html and "#e5533d" in html      # green save, red error
    assert "filter:" not in html and "click to filter by" not in html
    assert html.count('href="copytoast:') == 1           # the error's copy button only

    console._append_message("IO", "[IO] Door1.Open fired 105 times")
    assert 'href="filter:Door1"' in console.console.toHtml()       # others still link

    entry = get_debug_logger().get_buffer()[-1]
    assert console_plain_text(*entry) == "Could not write maps/room 12.json"   # condump
    console.deleteLater()


def test_a_console_toast_s_copy_button_copies_its_message(qt_app):
    from PyQt5.QtCore import QUrl
    from PyQt5.QtTest import QTest
    from editor.debug_console import debug_log_toast
    console = DebugConsole()
    console.clear()
    debug_log_toast("Could not write maps/room 12.json", 'error')
    QTest.qWait(200)
    import re
    link = re.search(r'href="(copytoast:\d+)"', console.console.toHtml()).group(1)
    QApplication.clipboard().setText("")
    console._on_anchor_clicked(QUrl(link))
    assert QApplication.clipboard().text() == "Could not write maps/room 12.json"
    console.deleteLater()


def test_the_copy_button_is_an_orange_i_in_a_circle():
    from editor.debug_console import _toast_copy_image
    image = _toast_copy_image(32)
    centre_column = [image.pixelColor(16, y) for y in range(32)]
    orange = [c for c in centre_column if c.alpha() > 128]
    assert orange and all(c.red() > 200 and 90 < c.green() < 160 and c.blue() < 60
                          for c in orange)
    assert image.pixelColor(2, 2).alpha() == 0           # round: the corner is clear


def test_outside_text_cannot_pass_for_a_toast_line(qt_app):
    """The no-links marker is made of reserved characters, stripped from
    anything else logged -- a map's text cannot switch its links off."""
    from editor.debug_console import debug_log, _NO_LINKS
    console = DebugConsole()
    console.clear()
    from PyQt5.QtTest import QTest
    debug_log("IO", _NO_LINKS + "Door1.Open fired 105 times")
    QTest.qWait(200)
    html = console.console.toHtml()
    assert '\x02' not in html and '\x03' not in html  # the marker's characters went
    assert 'href="filter:' in html                      # and the line still links
    console.deleteLater()


def test_a_multi_line_console_toast_is_as_wide_as_its_longest_line(qt_app):
    """Lines joined with <br> made Qt size the box as if they were one line,
    far wider than the longest; each line is now a paragraph of its own, and
    an indented line keeps its indent."""
    from PyQt5.QtTest import QTest
    from PyQt5.QtGui import QTextTable
    from editor.debug_console import debug_log_toast
    console = DebugConsole()
    console.resize(1200, 400)
    console.show()
    console.clear()
    debug_log_toast("Package export failed:\n  missing texture decals/Signage.png\n"
                    "  missing model models/crate.obj", 'warning')
    QTest.qWait(300)
    document = console.console.document()
    frames = [f for f in document.rootFrame().childFrames() if isinstance(f, QTextTable)]
    assert len(frames) == 1
    box = document.documentLayout().frameBoundingRect(frames[0])
    metrics = console.console.fontMetrics()
    longest = metrics.horizontalAdvance("  missing texture decals/Signage.png")
    assert box.width() < longest * 1.6          # not the width of all three lines
    text = console.console.toPlainText().replace('\xa0', ' ')
    assert "  missing texture decals/Signage.png" in text      # indent kept
    console.deleteLater()


def test_no_filter_hides_a_toast(qt_app):
    """Toasts are always in the console: a category filter, an entity filter
    or a hide box leaves them showing (while it hides ordinary lines)."""
    from PyQt5.QtTest import QTest
    from editor.debug_console import debug_log, debug_log_toast
    console = DebugConsole()
    console.clear()
    debug_log("IO", "Door1.Open fired")
    debug_log_toast("Saved level.json", 'success')
    debug_log_toast("Could not write maps/room.json", 'error')
    QTest.qWait(200)

    def shown():
        QTest.qWait(200)
        return console.console.toPlainText()

    console.filter_combo.setCurrentText("Error")          # another category
    text = shown()
    assert "Saved level.json" in text and "Could not write maps/room.json" in text
    assert "Door1.Open fired" not in text                 # ordinary lines filtered
    console._apply_entity_filter("Light_7")               # an entity filter
    text = shown()
    assert "Saved level.json" in text and "Could not write maps/room.json" in text
    console.filter_combo.setCurrentText("All")
    console.hide_movers_cb.setChecked(True)
    console.hide_triggers_cb.setChecked(True)
    text = shown()
    assert "Saved level.json" in text and "Could not write maps/room.json" in text
    console.deleteLater()
