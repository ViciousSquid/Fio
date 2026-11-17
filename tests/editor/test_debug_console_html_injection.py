"""Text from a map is shown in the Debug Console as text, never as HTML.

Entity names and a LogicCommand's command are logged verbatim. The console
used to keep any tag already in a message and insert it with ``insertHtml``:
``<img src=...>`` made the widget open a file (or a UNC path), ``<a href>``
put a link with any label on it in front of the user. Only a line logged
with ``debug_log_markup`` -- markup written in Fio's own code -- keeps tags.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from editor import debug_console as dc  # noqa: E402
from editor.debug_console import (  # noqa: E402
    DebugConsole, console_entity_link, console_escape, debug_log, debug_log_markup,
    debug_log_raw, get_debug_logger,
)

pytestmark = pytest.mark.qt

IMG = '<img src="/tmp/probe.png">'
LINK = '<a href="https://example.invalid/update">Fio update available - click</a>'


@pytest.fixture(scope="module")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def console(qt_app):
    widget = DebugConsole()
    widget.clear()
    loads = []
    original = widget.console.loadResource

    def spy(kind, url):
        loads.append(url.toString())
        return original(kind, url)

    widget.console.loadResource = spy
    widget.resource_loads = loads
    yield widget
    widget.deleteLater()


def _show(console, category, message):
    """Insert one line and lay the document out, as showing it would."""
    line = console._format_message(category, message)
    console._insert_lines([line])
    console.console.document().documentLayout().documentSize()
    return line, console.console.toHtml(), console.console.toPlainText()


@pytest.mark.parametrize("category", ["IO", "Error", "Warning", "Plugins", "MonsterAI", "Info", ""])
def test_a_logic_command_cannot_put_an_image_or_a_link_in_the_console(console, category):
    # Exactly what editor/io_handlers.py logs for a LogicCommand.
    message = f"[{category}] LogicCommand 'cmd1': queued 'message hi {IMG}{LINK}'"
    line, document, text = _show(console, category, message)
    assert "<img" not in line and "<img" not in document, f"live <img> in {category!r}: {line}"
    assert console.resource_loads == [], f"the console loaded {console.resource_loads}"
    assert 'href="https://example.invalid/update">Fio update' not in line, (
        f"a link with a label of the map's choosing is live: {line}")
    assert IMG in text, f"the text must be shown as written, got: {text!r}"
    assert "Fio update available - click</a>" in text


@pytest.mark.parametrize("name", [
    '<img src="\\\\evil-host\\share\\x.png">',
    'x"><img src=/etc/passwd>',
    "<b>bold</b><table><tr><td>cell",
    "a & b < c > d",
])
def test_an_entity_name_is_shown_literally(console, name):
    _line, document, text = _show(console, "IO", f"[IO] Trigger '{name}' touched by player")
    assert name in text, f"expected the name as written in {text!r}"
    assert "<img" not in document and "<table" not in document
    assert console.resource_loads == []


def test_the_markup_marker_cannot_be_forged_by_logged_text(console):
    logger = get_debug_logger()
    logger.clear_buffer()
    hostile = dc._MARKUP + IMG
    debug_log("IO", hostile)
    debug_log_raw(hostile)
    for category, message in logger.get_buffer():
        assert not message.startswith(dc._MARKUP) and dc._MARKUP not in message, (
            f"untrusted text kept the markup marker: {message!r}")
        assert "<img" not in console._format_message(category, message)
    logger.clear_buffer()


def test_debug_log_markup_keeps_fios_own_colours_and_filter_links(console):
    logger = get_debug_logger()
    logger.clear_buffer()
    debug_log_markup("MonsterAI",
                     f'{console_entity_link("Guard_1", "#EF5350")} '
                     f'<span style="color: #B71C1C; font-weight: bold;">DIED</span>')
    (category, message), = logger.get_buffer()
    logger.clear_buffer()
    line, document, text = _show(console, category, message)
    assert '<a href="filter:Guard_1"' in line and line.count("filter:Guard_1") == 1, line
    assert "#b71c1c" in document.lower(), "the DIED colour was lost"
    assert "Guard_1 DIED" in text and "[MonsterAI]" in text
    assert "\x02" not in text and "\x03" not in text


def test_a_hostile_name_inside_trusted_markup_is_escaped_by_the_helpers():
    name = 'x"><img src=/etc/passwd>'
    link = console_entity_link(name)
    assert "<img" not in link and "&lt;img" in link
    assert console_escape("<b>&\"'") == "&lt;b&gt;&amp;&quot;&#x27;"


def test_monster_ai_lines_keep_their_colour_but_not_a_hostile_name(console):
    # The shape engine/monster_ai.py logs, with a well-behaved name ...
    style = "color: #EF5350; font-weight: bold; text-decoration: none;"
    good = (f'[MonsterAI] <a href="filter:Guard_1" style="{style}">Guard_1</a> '
            f'<span style="color: #B71C1C; font-weight: bold;">DIED</span> (shot by player)')
    line, document, text = _show(console, "MonsterAI", good)
    assert f'<a href="filter:Guard_1" style="{style}">Guard_1</a>' in line, line
    assert "#b71c1c" in document.lower()
    assert "Guard_1 DIED (shot by player)" in text
    # ... and with a name that tries to be markup.
    name = f'{IMG}{LINK}'
    bad = (f'[MonsterAI] <a href="filter:{name}" style="{style}">{name}</a> '
           f'<span style="color: #B71C1C; font-weight: bold;">DIED</span>')
    line, document, text = _show(console, "MonsterAI", bad)
    assert "<img" not in line and "<img" not in document, line
    assert 'href="https://example.invalid/update">Fio update' not in line, line
    assert console.resource_loads == []
    assert IMG in text


@pytest.mark.parametrize("tag", [
    '<img src="xpng">', '<a href="https://example.invalid/">x</a>',
    '<a href="file:///etc/passwd" style="color: red;">x</a>',
    '<span style="background: url(xpng)">x</span>',
    '<span style="color: red" onclick="x">x</span>', '<table>', '<style>', '<link rel="x">',
    '<object data="x">', '<b style="color: red; background-image: url(x)">',
])
def test_only_inert_formatting_survives_in_the_legacy_categories(console, tag):
    import re
    line = console._format_message("Info", f"[Info] before {tag} after")
    # Escaped text holds no "<": whatever still parses as a tag is live.
    inert = re.compile(
        r'</?(?:b|i|span|a)>|<br>'
        r'|<(?:span|b) style="color: ?#?\w+;( font-weight: bold;)?">'
        r'|<a href="https://example\.invalid/" style="color: #26A69A; text-decoration: underline;" '
        r'title="Open this link in your browser">')
    live = re.findall(r'<[^>]*>', line)
    unexpected = [t for t in live if not inert.fullmatch(t)]
    assert not unexpected, f"{tag!r} left live markup {unexpected} in: {line}"
    for needle in ('url(', 'onclick', 'file:', 'src=', 'data=', 'rel=', 'background'):
        assert not any(needle in t for t in live), f"{needle!r} is inside a live tag: {line}"


def test_an_unclosed_tag_cannot_restyle_the_following_lines(console):
    line = console._format_message("Info", "[Info] <b>never closed <i>twice")
    assert line.count("<b>") == line.count("</b>") == 1, line
    assert line.count("<i>") == line.count("</i>") == 1, line
    stray = console._format_message("Info", "[Info] early </span> close")
    assert stray.count("</span>") == 1, f"a stray closer ended the line's own span: {stray}"


def test_help_text_markup_and_entities_still_render(console):
    help_text = ('[Info] <i>Commands:</i><br><b style="color:orange;">map</b> &lt;name&gt; '
                 '— Load a map<br><b style="color:orange;">message</b> &quot;text&quot;')
    _line, document, text = _show(console, "Info", help_text)
    assert "map <name>" in text and 'message "text"' in text, text
    assert "font-weight:600" in document or "font-weight:700" in document or "<b" in document


def test_usage_text_with_angle_brackets_is_readable(console):
    _line, _document, text = _show(console, "Error", "[Error] Usage: r_fogcolor <r> <g> <b>   (0-255)")
    assert "r_fogcolor <r> <g> <b>" in text, f"placeholders were swallowed as tags: {text!r}"


def test_the_consoles_own_links_and_highlights_still_work(console):
    line = console._format_message("IO", "[IO] Door_1.Open -> Trigger_2 (type=trigger) 'Relay_3'")
    for name in ("Door_1", "Trigger_2", "Relay_3"):
        assert f'href="filter:{name}"' in line, f"no filter link for {name}: {line}"
    assert "→" in line and "-&gt;" not in line, "the arrow highlight was lost"
    raw = console._format_message("", "github.com/vicioussquid/Fio")
    assert 'href="https://github.com/vicioussquid/Fio"' in raw and "Open Fio on GitHub" in raw
    other = console._format_message("IO", "[IO] see https://example.invalid/a?b=1&c=2 now")
    assert 'href="https://example.invalid/a?b=1&amp;c=2"' in other, other
    assert "Open Fio on GitHub" not in other, "a foreign link must not be labelled as Fio's"
    quoted = console._format_message("IO", '[IO] https://example.invalid/"onmouseover="x')
    assert 'href="https://example.invalid/"' in quoted and 'onmouseover="x"' not in quoted


def test_a_typed_command_is_echoed_as_text(console):
    console.command_input.setText('say <img src="/tmp/probe.png"> hi')
    console._on_command_entered()
    assert "<img" not in console.console.toHtml()
    assert 'say <img src="/tmp/probe.png"> hi' in console.console.toPlainText()
    assert console.resource_loads == []
