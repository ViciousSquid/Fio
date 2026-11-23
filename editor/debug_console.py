from engine.asset_qt import load_pixmap
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextBrowser, QPushButton, 
    QLabel, QCheckBox, QComboBox, QFrame, QLineEdit, QSplitter, QScrollArea, QApplication
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QUrl, QTimer, QEvent
from PyQt5 import sip
from PyQt5.QtGui import QFont, QTextCursor, QPainter, QPixmap, QDesktopServices
from collections import deque
import html
import os
import re


#: Marks a buffered console line whose text is Fio's own HTML. Only
#: :meth:`DebugLogger.log_markup` puts it there; ``log``/``log_raw`` remove
#: these control characters from what they are given, so text from a map (an
#: entity name, a LogicCommand's command) can never claim to be markup.
_MARKUP = '\x02fio-markup\x03'
#: Marks a toast written to the console (every toast is): ``_TOAST``,
#: then ``level|serial|message``. Reserved characters, like _MARKUP: no
#: outside text can carry it.
_TOAST = '\x02fio-toast\x03'

#: A console toast's border, text, background and icon, by level (Fio's
#: colours: success green, warning orange; error red, info blue).
TOAST_STYLES = {
    'success': ('#4CAF50', '#8fdc98', '#14231a', '\u2714'),
    'info':    ('#3d8ee8', '#82bdf6', '#121f2d', 'i'),
    'warning': ('#F08000', '#f7b866', '#2a1e0f', '!'),
    'error':   ('#e5533d', '#f3836f', '#2b1613', '\u2716'),
}
#: The copy button's image in the console document, and its link scheme.
_TOAST_COPY_IMAGE = 'fio-toast-copy'
_TOAST_COPY_LINK = 'copytoast:'
_TOAST_CLOSE_LINK = 'closetoast:'
_toast_serials = __import__('itertools').count(1)
#: Seconds a console toast stays in full colour before it turns faint (it is
#: never removed by itself: only its close button removes it).
TOAST_FADE_SECONDS = 3.0
#: How far a faint (dormant) card's colours sink toward the console
#: background: well back, so a card that is new or hovered stands out.
TOAST_FAINT = 0.8
#: Seconds a card takes to light up (hovered) or dim again: a fade, both
#: ways, never a jump.
TOAST_TRANSITION_SECONDS = 0.5
#: A toast with the same level and words as one logged less than this many
#: seconds before (its last repeat) is not a new card: that card counts it,
#: shows the count (``×3``), and turns full colour again.
TOAST_REPEAT_SECONDS = 2.0
_CONSOLE_BG = '#1a1a1a'


def _mix(colour, toward, amount):
    """*colour* moved *amount* (0..1) of the way to *toward* (hex strings)."""
    from PyQt5.QtGui import QColor
    a, b = QColor(colour), QColor(toward)
    return QColor(round(a.red() + (b.red() - a.red()) * amount),
                  round(a.green() + (b.green() - a.green()) * amount),
                  round(a.blue() + (b.blue() - a.blue()) * amount))


def _toast_copy_image(size=32):
    """The console toast's copy button: an orange "i" in an orange circle."""
    from PyQt5.QtGui import QImage, QPainter, QPen, QColor
    from PyQt5.QtCore import Qt, QPointF, QRectF
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    orange = QColor('#F08000')
    unit = size / 16.0
    pen = QPen(orange, 1.4 * unit)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QRectF(1.2 * unit, 1.2 * unit, 13.6 * unit, 13.6 * unit))
    painter.drawLine(QPointF(8 * unit, 7 * unit), QPointF(8 * unit, 11.6 * unit))
    painter.setPen(Qt.NoPen)
    painter.setBrush(orange)
    painter.drawEllipse(QPointF(8 * unit, 4.6 * unit), 1.0 * unit, 1.0 * unit)
    painter.end()
    return image


def _toast_parts(message):
    """``(level, serial, text)`` of a logged toast entry's message."""
    level, _, rest = message[len(_TOAST):].partition('|')
    serial, _, text = rest.partition('|')
    return level, serial, text


#: Marks a line whose words are never links (no click-to-filter on its
#: names, numbers or values): a toast written to the console. Like _MARKUP it
#: is made of reserved characters, so no outside text can carry it.
_NO_LINKS = '\x02fio-no-links\x03'
_MARKUP_CHARS = str.maketrans('', '', '\x00\x02\x03')


def console_escape(text) -> str:
    """*text* made inert for the console: use it for every value (an entity
    name, a file name) placed inside a :func:`debug_log_markup` message."""
    return html.escape(str(text), quote=True)


def console_entity_link(name, color='#F08000') -> str:
    """The console's clickable entity-filter link for *name*, escaped."""
    safe = console_escape(name)
    return (f'<a href="filter:{safe}" style="color: {color}; font-weight: bold; '
            f'text-decoration: none;">{safe}</a>')


#: Categories whose callers still write formatting straight into
#: ``debug_log`` (MonsterAI's coloured names, the console's help and listing
#: text). In these, and only these, the inert tags below survive; any other
#: tag -- ``<img>``, a link to anywhere, an attribute not listed -- is shown
#: as text, as it is in every other category. A caller that moves to
#: :func:`debug_log_markup` no longer needs its category here.
_INERT_MARKUP_CATEGORIES = frozenset({'MonsterAI', 'Info'})

_INERT_STYLE = (r'(?:\s*(?:color\s*:\s*(?:#[0-9a-fA-F]{3,8}|[a-zA-Z]{1,20})'
                r'|font-weight\s*:\s*bold|text-decoration\s*:\s*none)\s*;?)+')
#: Formatting only: no tag here can load a resource or lead anywhere but
#: the console's own entity filter.
_RE_INERT_TAG = re.compile(
    r'<(?:(?P<void>br)\s*/?'
    r'|(?P<close>/(?:b|i|u|span|a))'
    r'|(?P<open>b|i|u)'
    r'|(?P<styled>b|i|span) style="' + _INERT_STYLE + r'"'
    r'|(?P<link>a) href="filter:[^"<>&\x00]{0,80}" style="' + _INERT_STYLE + r'"'
    r')>')
_RE_BARE_AMP = re.compile(r'&(?!(?:[a-zA-Z]{2,8}|#[0-9]{1,6}|#x[0-9a-fA-F]{1,5});)')


def _escape_text(text) -> str:
    """Escape ``<``, ``>`` and any ``&`` that is not a character reference."""
    return _RE_BARE_AMP.sub('&amp;', text).replace('<', '&lt;').replace('>', '&gt;')


def _inert_markup(message, protect) -> str:
    """*message* with everything escaped except balanced inert formatting.

    *protect* is called with each surviving tag and returns what stands in
    for it. A closing tag with nothing open is text; a tag left open is
    closed at the end, so one line cannot restyle the lines after it.
    """
    out, stack, pos = [], [], 0
    for match in _RE_INERT_TAG.finditer(message):
        out.append(_escape_text(message[pos:match.start()]))
        pos = match.end()
        tag = match.group(0)
        closing = match.group('close')
        if closing:
            if not stack or stack[-1] != closing[1:]:
                out.append(_escape_text(tag))
                continue
            stack.pop()
        elif not match.group('void'):
            stack.append(match.group('open') or match.group('styled') or match.group('link'))
        out.append(protect(tag))
    out.append(_escape_text(message[pos:]))
    out.extend(protect(f'</{name}>') for name in reversed(stack))
    return ''.join(out)


def _plain(message) -> str:
    """Untrusted console text: without the characters reserved for markup."""
    return str(message).translate(_MARKUP_CHARS)


#: How many messages the console history (the logger's buffer) keeps; older
#: ones drop off the front. It is what a rebuilt console shows again and what
#: ``condump`` writes. Kept well under DebugConsole.MAX_DOCUMENT_LINES, which
#: rebuilds the document from this buffer when exceeded.
HISTORY_LIMIT = 1000

#: The buffer category of a command line typed into a console, echoed as
#: ``] command``. Not a log category: it has no filter of its own.
ECHO_CATEGORY = '\x02echo\x03'
_ECHO_PREFIX = '] '
_ECHO_HTML = "<br><span style='color: #F08000; font-weight: bold;'>] {}</span><br>"

_RE_ANY_TAG = re.compile(r'</?[a-zA-Z][^>]*>')
_RE_BR = re.compile(r'<br\s*/?>', re.IGNORECASE)


def _html_to_text(text, tag_re) -> str:
    """Console HTML as the plain text it shows: ``<br>`` a line break, the
    other tags *tag_re* matches dropped, character references decoded."""
    if tag_re.search(text):
        # Rendered as HTML: source line breaks are only white space.
        text = re.sub(r'[ \t]*\n\s*', ' ', text)
        text = _RE_BR.sub('\n', text)
        text = tag_re.sub('', text)
        text = '\n'.join(line.strip() for line in text.split('\n')).strip('\n')
    return html.unescape(text)


def console_plain_text(category, message) -> str:
    """One console history entry as the plain text the console shows for it.

    Mirrors :meth:`DebugConsole._format_message`: a line logged with
    :func:`debug_log_markup` (and the inert formatting of the Info and
    MonsterAI categories) is turned from HTML into text; any other line is
    shown literally, so it is returned as it is.
    """
    message = str(message)
    if category == ECHO_CATEGORY:
        return message
    if message.startswith(_TOAST):
        _level, serial, text = _toast_parts(message)
        count = get_debug_logger().toast_count(serial)
        return text.translate(_MARKUP_CHARS) + (f'  \u00d7{count}' if count > 1 else '')
    if message.startswith(_NO_LINKS):
        return message[len(_NO_LINKS):].translate(_MARKUP_CHARS)
    if message.startswith(_MARKUP):
        return _html_to_text(message[len(_MARKUP):].translate(_MARKUP_CHARS), _RE_ANY_TAG)
    message = message.translate(_MARKUP_CHARS)
    if (category or 'Info') in _INERT_MARKUP_CATEGORIES:
        return _html_to_text(message, _RE_INERT_TAG)
    return message


def write_console_dump(directory, entries, base='console', ext='.txt') -> str:
    """Write console history *entries* as plain text to a new file in
    *directory* and return its path.

    The file is ``console.txt``, or ``console_1.txt``, ``console_2.txt``...
    when that name is taken: an existing file is never overwritten (each name
    is claimed with an exclusive create, so not even by a concurrent dump).
    """
    os.makedirs(directory, exist_ok=True)
    text = ''.join(console_plain_text(category, message) + '\n'
                   for category, message in entries)
    index = 0
    while True:
        name = f'{base}{ext}' if index == 0 else f'{base}_{index}{ext}'
        path = os.path.join(directory, name)
        try:
            with open(path, 'x', encoding='utf-8', newline='\n') as handle:
                handle.write(text)
            return path
        except FileExistsError:
            index += 1


class DebugLogger(QObject):
    """
    Global singleton that collects debug messages and emits signals.
    This allows the io_system to log without direct widget dependencies.
    """
    message_logged = pyqtSignal(str, str)  # (category, message)
    #: A toast was logged again while its card was recent: (serial, count).
    toast_repeated = pyqtSignal(str, int)

    _instance = None

    def __new__(cls):
        # A QObject whose C++ side has been destroyed leaves its Python
        # wrapper behind.  Without this check the singleton hands that corpse
        # back for the rest of the process, and every later
        # `logger.message_logged.connect(...)` raises -- which means the
        # Debug Console can never be built again.
        if cls._instance is not None and sip.isdeleted(cls._instance):
            cls._instance = None
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        super().__init__()
        self._initialized = True
        self._enabled = True
        self._buffer = deque(maxlen=HISTORY_LIMIT)  # the console history
        #: (level, words) -> [serial, when last logged]: the recent toasts a
        #: repeat is counted on (TOAST_REPEAT_SECONDS).
        self._toast_recent = {}
        #: serial -> how many times that toast was logged, when more than once.
        self._toast_counts = {}
        self._toast_lock = __import__('threading').Lock()

    def log(self, category: str, message: str):
        """Log a message with a category tag."""
        if not self._enabled:
            return

        # Plain text: the console shows any HTML in it literally.
        full_msg = f"[{category}] {_plain(message)}"

        self._buffer.append((category, full_msg))
        self.message_logged.emit(category, full_msg)

    def log_unlinked(self, category: str, message: str):
        """Log a plain line the console draws without links: nothing in it
        (a number, a name, a value) becomes a click-to-filter link. For
        toasts written to the console when toasts are off."""
        if not self._enabled:
            return
        full_msg = f"{_NO_LINKS}[{category}] {_plain(message)}"
        self._buffer.append((category, full_msg))
        self.message_logged.emit(category, full_msg)

    def log_toast(self, message: str, level: str = 'info'):
        """Log a toast: drawn in the console as a card in its level's colour,
        a block of its own, an error's with a copy button, no links in it."""
        if not self._enabled:
            return
        level = level if level in TOAST_STYLES else 'info'
        words = _plain(message)
        key = (level, words)
        now = __import__('time').monotonic()
        with self._toast_lock:
            recent = self._toast_recent.get(key)
            if recent is not None and now - recent[1] < TOAST_REPEAT_SECONDS:
                # Again, and quickly: counted on its card, not a new one.
                recent[1] = now
                serial = recent[0]
                count = self._toast_counts.get(serial, 1) + 1
                self._toast_counts[serial] = count
            else:
                serial = str(next(_toast_serials))
                count = 0
                if len(self._toast_recent) > 64:
                    self._toast_recent = {
                        k: v for k, v in self._toast_recent.items()
                        if now - v[1] < TOAST_REPEAT_SECONDS}
                self._toast_recent[key] = [serial, now]
        if count:
            self.toast_repeated.emit(serial, count)
            return
        full_msg = f"{_TOAST}{level}|{serial}|{words}"
        self._buffer.append(('Info', full_msg))
        self.message_logged.emit('Info', full_msg)

    def toast_count(self, serial) -> int:
        """How many times toast *serial* was logged (its card's ``×N``)."""
        return self._toast_counts.get(str(serial), 1)

    def toasts(self):
        """``[(serial, level, text)]`` of the toasts in the history, oldest
        first."""
        found = []
        for _category, message in self._buffer:
            if str(message).startswith(_TOAST):
                level, serial, text = _toast_parts(message)
                found.append((serial, level, text))
        return found

    def remove_toast(self, serial):
        """Take the toast logged as *serial* out of the history (its close
        button): a rebuilt console does not bring it back."""
        serial = str(serial)
        with self._toast_lock:
            self._toast_counts.pop(serial, None)
            self._toast_recent = {k: v for k, v in self._toast_recent.items()
                                  if v[0] != serial}
        kept = [entry for entry in self._buffer
                if not (str(entry[1]).startswith(_TOAST)
                        and _toast_parts(entry[1])[1] == serial)]
        if len(kept) != len(self._buffer):
            self._buffer.clear()
            self._buffer.extend(kept)
            return True
        return False

    def toast_text(self, serial) -> str:
        """The message of the toast logged as *serial* (its copy button), or
        None once it has left the history."""
        for category, message in reversed(self._buffer):
            if str(message).startswith(_TOAST):
                _level, number, text = _toast_parts(message)
                if number == str(serial):
                    return text
        return None

    def log_markup(self, category: str, markup: str):
        """Log a line that is Fio's own HTML (colours, entity-filter links).

        Only for markup written in code. Every value interpolated into it
        must go through :func:`console_escape` (or
        :func:`console_entity_link`); a message that merely contains outside
        text belongs in :meth:`log`, which the console escapes as a whole.
        """
        if not self._enabled:
            return

        full_msg = f"{_MARKUP}[{console_escape(category)}] {_plain(markup)}"

        self._buffer.append((category, full_msg))
        self.message_logged.emit(category, full_msg)

    def log_raw(self, message: str):
        """Log a console line without adding a category prefix."""
        if not self._enabled:
            return

        message = _plain(message)
        self._buffer.append(('', message))
        self.message_logged.emit('', message)

    def log_command_echo(self, command: str):
        """Keep a command typed into a console (shown there as ``] command``)
        in the history, so a rebuilt console and ``condump`` have it too.

        Not emitted: the console that took the command has drawn it already.
        """
        if not self._enabled:
            return
        self._buffer.append((ECHO_CATEGORY, _ECHO_PREFIX + _plain(command)))

    @staticmethod
    def is_command_echo(entry, command=None) -> bool:
        """Whether history *entry* is a typed command's echo (of *command*,
        matched by its first word, when given)."""
        category, message = entry
        if category != ECHO_CATEGORY:
            return False
        if command is None:
            return True
        words = str(message)[len(_ECHO_PREFIX):].split(maxsplit=1)
        return bool(words) and words[0].lower() == command.lower()

    def history_limit(self) -> int:
        """How many messages the history keeps (older ones drop off)."""
        return self._buffer.maxlen

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def get_buffer(self):
        """Get all buffered messages."""
        return list(self._buffer)

    def clear_buffer(self):
        """Clear the message buffer."""
        self._buffer.clear()
        with self._toast_lock:
            self._toast_recent = {}
            self._toast_counts = {}


# Global logger instance
_debug_logger = None

def get_debug_logger() -> DebugLogger:
    """Get the global debug logger instance, rebuilding a destroyed one."""
    global _debug_logger
    if _debug_logger is None or sip.isdeleted(_debug_logger):
        _debug_logger = DebugLogger()
    return _debug_logger


def debug_log(category: str, message: str):
    """Convenience function to log a debug message."""
    get_debug_logger().log(category, message)


def debug_log_toast(message: str, level: str = 'info'):
    """A toast in the console: a card in *level*'s colour (red errors, green
    successes...) -- a tinted body and an accent bar -- a block of its own, an
    error's with a copy button, and no click-to-filter links in it."""
    get_debug_logger().log_toast(message, level)


def debug_log_markup(category: str, markup: str):
    """Log a line of Fio's own console HTML; see :meth:`DebugLogger.log_markup`."""
    get_debug_logger().log_markup(category, markup)


def debug_log_raw(message: str):
    """Convenience function for an unprefixed console line."""
    get_debug_logger().log_raw(message)


def install_excepthook():
    """Report exceptions that escape Qt callbacks instead of aborting.

    PyQt5 calls ``qFatal`` -- ending the process -- when an exception escapes
    a slot or a Qt virtual (``paintGL``, an event handler) and no
    ``sys.excepthook`` is installed. One bad console argument or a paint error
    would take the open, unsaved map with it. This prints the traceback, puts
    it in the Debug Console, and lets the event loop carry on.
    """
    import sys
    import time
    import traceback

    # An exception raised by the same code every frame (a paint, a timer)
    # would otherwise print its whole traceback 60 times a second and push
    # everything else out of the console within seconds. An identical
    # traceback within REPEAT_WINDOW of the last is counted, not printed.
    REPEAT_WINDOW = 2.0
    last = {'text': None, 'at': 0.0, 'repeats': 0}

    def _report(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        now = time.monotonic()
        if text == last['text'] and now - last['at'] < REPEAT_WINDOW:
            last['at'] = now
            last['repeats'] += 1
            return
        repeats = last['repeats']
        last.update(text=text, at=now, repeats=0)
        if repeats:
            text = f"(the previous exception repeated {repeats} more time(s))\n" + text
        sys.stderr.write(text)
        try:
            debug_log("Error", "Unhandled exception:\n" + text)
        except Exception:
            pass

    sys.excepthook = _report
    return _report


#: Commands run from any console, oldest first, shared by every command input
#: so Up repeats the last command wherever it was typed.
COMMAND_HISTORY = []


class CommandInput(QLineEdit):
    """Command line with Quake-style history: Up recalls the last command.

    All command inputs share :data:`COMMAND_HISTORY` unless given their own
    list; each keeps its own place while browsing it.
    """
    def __init__(self, parent=None, history=None):
        super().__init__(parent)
        self.history = COMMAND_HISTORY if history is None else history
        self.history_idx = len(self.history)

    def reset_history_position(self):
        """Start browsing from the newest command again (on open/clear)."""
        self.history_idx = len(self.history)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Up:
            if self.history and self.history_idx > 0:
                self.history_idx -= 1
                self.setText(self.history[self.history_idx])
        elif event.key() == Qt.Key_Down:
            if self.history and self.history_idx < len(self.history) - 1:
                self.history_idx += 1
                self.setText(self.history[self.history_idx])
            elif self.history_idx == len(self.history) - 1:
                self.history_idx = len(self.history)
                self.clear()
        else:
            super().keyPressEvent(event)

    def add_history(self, command):
        if command and (not self.history or self.history[-1] != command):
            self.history.append(command)
        self.history_idx = len(self.history)


class LogoTextBrowser(QTextBrowser):
    """Custom QTextBrowser that paints a centered logo with 80% transparency behind the text."""

    def __init__(self, logo_path=None, parent=None):
        super().__init__(parent)
        self._logo_path = logo_path
        self._logo_pixmap = None
        self._logo_opacity = 0.20  # 80% transparent = 20% opaque
        self._load_logo()

    def set_logo_path(self, logo_path):
        """Update the logo path and reload."""
        self._logo_path = logo_path
        self._load_logo()
        self.viewport().update()

    def _load_logo(self):
        """Load the logo pixmap if path is valid."""
        if self._logo_path:
            self._logo_pixmap = load_pixmap(self._logo_path)
        else:
            self._logo_pixmap = None

    def paintEvent(self, event):
        """Paint the logo centered with 80% transparency, then paint text on top."""
        painter = QPainter(self.viewport())

        # Paint the logo first (behind text)
        if self._logo_pixmap and not self._logo_pixmap.isNull():
            # Calculate centered position
            vp_rect = self.viewport().rect()
            img_w = self._logo_pixmap.width()
            img_h = self._logo_pixmap.height()

            x = (vp_rect.width() - img_w) // 2
            y = (vp_rect.height() - img_h) // 2

            # Save painter state
            painter.save()
            painter.setOpacity(self._logo_opacity)
            painter.drawPixmap(x, y, self._logo_pixmap)
            painter.restore()

        painter.end()

        # Let QTextBrowser paint the text content on top
        super().paintEvent(event)


class DebugConsole(QWidget):
    """
    Console window for viewing I/O and entity logic messages.
    """
    command_issued = pyqtSignal(str) 

    # Category colors
    CATEGORY_COLORS = {
        'IO': '#4FC3F7',       # Light blue
        'Speaker': '#81C784',   # Light green
        'Trigger': '#FFB74D',   # Orange
        'Door': '#BA68C8',      # Purple
        'Timer': '#F06292',     # Pink
        'Error': '#EF5350',     # Red
        'Warning': '#FFEE58',   # Yellow
        'Info': '#FFFFFF',      # White
        'MonsterAI': '#FF7043', # Deep orange — monster combat / sight / attack
        'Pathfinding': '#26A69A', # Teal — monster patrol / navigation
        'Plugins': '#42A5F5',   # Blue — plugin debug / init (default; loads/errors recoloured per-message)
        'Cutscene': '#66BB6A',  # Cutscene loads/errors are recoloured per-message
    }

    # Per-message colours for the 'Plugins' category. All plugin messages share
    # one category, so they are recoloured by content: successful loads green,
    # failures red, and everything else (debug / init chatter) the blue default.
    PLUGIN_LOAD_COLOR  = '#66BB6A'  # Green — loaded / enabled / bundled
    PLUGIN_ERROR_COLOR = '#EF5350'  # Red — failures / errors
    PLUGIN_DEBUG_COLOR = '#42A5F5'  # Blue — debug / init messages
    CUTSCENE_LOAD_COLOR = '#66BB6A'  # Green — successful cutscene load
    CUTSCENE_ERROR_COLOR = '#EF5350'  # Red — missing/invalid/failed cutscene

    # Substrings that mark a plugin message as an error (red) or a load (green).
    # Checked case-insensitively; errors take precedence over loads.
    _PLUGIN_ERROR_HINTS = ('fail', 'error', 'exception', 'traceback',
                           'not a fioplugin', 'exposes no', 'skipped')
    _PLUGIN_LOAD_HINTS  = ('load', 'enabled', 'bundled', 'register')

    FONT_SIZE_MIN = 6
    FONT_SIZE_MAX = 24
    FONT_SIZE_DEFAULT = 9

    #: The toolbar's buttons (-, +, Clear, the filter menu) share one width.
    TOOLBAR_BUTTON_WIDTH = 52
    #: The filter-menu button: Fio orange, as the selected tab is.
    FILTER_BUTTON_STYLE = """
        QPushButton {
            background-color: #F08000;
            color: white;
            border: 1px solid #F08000;
            border-radius: 3px;
            padding: 3px 6px;
            font-weight: bold;
        }
        QPushButton:hover { background-color: #FF9A2E; border-color: #FF9A2E; }
        QPushButton:pressed { background-color: #C06600; }
    """
    #: ...and while the filter column is open: pressed in, a darker orange.
    FILTER_BUTTON_OPEN_STYLE = """
        QPushButton {
            background-color: #C06600;
            color: white;
            border: 1px solid #FFB060;
            border-radius: 3px;
            padding: 3px 6px;
            font-weight: bold;
        }
        QPushButton:hover { background-color: #D87200; }
        QPushButton:pressed { background-color: #A05500; }
    """

    # Pre-compiled regex patterns (avoids re.compile on every _append_message call)
    _RE_IO_ENTITY    = re.compile(r'^\[IO\]\s+([a-zA-Z0-9_]+)\.')
    _RE_ENTITY_DOT   = re.compile(r'(?<!=)\b([a-zA-Z0-9_]+)(?=\.)')
    _RE_ENTITY_TYPE  = re.compile(r'\b([a-zA-Z0-9_]+)(?=\s+\(type=)')
    _RE_ENTITY_QUOTE = re.compile(r"'([a-zA-Z0-9_]+)'")
    _RE_HELP_QUOTE   = re.compile(r"(['\"])help\1")
    _RE_FIRE_OUTPUT  = re.compile(r'\b(fire_output)\b')
    _RE_NO_CONNS     = re.compile(r'(no connections|0 connections)')
    _RE_DELAYED      = re.compile(r'\[Delayed\]')
    _RE_ARROW        = re.compile(r' -(?:>|&gt;) ')
    _instance = None

    @classmethod
    def get_instance(cls, parent=None, logo_path="assets/logo.png"):
        """Return the singleton DebugConsole, creating it on first call.

        A console whose widget has been destroyed is rebuilt rather than
        handed back dead, for the same reason as the logger above.
        """
        if cls._instance is not None and sip.isdeleted(cls._instance):
            cls._instance = None
        if cls._instance is None:
            cls._instance = cls(parent, logo_path)
        return cls._instance

    def __init__(self, parent=None, logo_path="assets/logo.png"):
        super().__init__(parent)
        self._logo_path = logo_path
        # Track enabled categories
        self.enabled_categories = set(self.CATEGORY_COLORS.keys())

        # Message count
        self.message_count = 0
        #: Lines currently in the document (bounded; see MAX_DOCUMENT_LINES).
        self._document_lines = 0
        #: Logged messages not inserted yet; see FLUSH_INTERVAL_MS.
        self._pending = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(self.FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_pending)

        # Current Entity Filter (None means show all)
        self.active_entity_filter = None

        # Font size
        self.font_size = self.FONT_SIZE_DEFAULT

        self._setup_ui()
        self._connect_logger()

        # Load any buffered messages
        self._load_buffer()

    def _setup_ui(self):
        """Set up the console UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Top toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        # Filter label
        filter_label = QLabel("Filter:")
        filter_label.setStyleSheet("color: #888; font-weight: bold;")
        toolbar.addWidget(filter_label)

        # Category filter dropdown
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("All")
        for cat in sorted(self.CATEGORY_COLORS.keys()):
            self.filter_combo.addItem(cat)
        self.filter_combo.setCurrentText("All")
        self.filter_combo.currentTextChanged.connect(self._on_filter_changed)
        self.filter_combo.setMinimumWidth(200)
        toolbar.addWidget(self.filter_combo)

        # Filter Empty Checkbox
        self.filter_empty_cb = QCheckBox("Filter Empty")
        self.filter_empty_cb.setToolTip("Hide messages about 0 connections")
        self.filter_empty_cb.setChecked(True) 
        self.filter_empty_cb.toggled.connect(self._refresh_console)
        toolbar.addWidget(self.filter_empty_cb)

        # Separator
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setStyleSheet("color: #444;")
        toolbar.addWidget(sep2)

        # Font size controls (the size itself is in their tooltips)
        font_btn_style = """
            QPushButton {
                background-color: #444;
                color: #ccc;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 3px 6px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #555;
                border-color: #F08000;
            }
            QPushButton:pressed {
                background-color: #333;
            }
        """

        decrease_btn = QPushButton("−")
        decrease_btn.setFixedWidth(self.TOOLBAR_BUTTON_WIDTH)
        decrease_btn.clicked.connect(self._decrease_font_size)
        decrease_btn.setStyleSheet(font_btn_style)
        toolbar.addWidget(decrease_btn)
        self._decrease_btn = decrease_btn

        increase_btn = QPushButton("+")
        increase_btn.setFixedWidth(self.TOOLBAR_BUTTON_WIDTH)
        increase_btn.clicked.connect(self._increase_font_size)
        increase_btn.setStyleSheet(font_btn_style)
        toolbar.addWidget(increase_btn)
        self._increase_btn = increase_btn
        self._show_font_size()

        # Clear button (the message count is in its tooltip)
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(self.TOOLBAR_BUTTON_WIDTH)
        clear_btn.clicked.connect(self.clear)
        self._clear_btn = clear_btn
        self._show_message_count()
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #444;
                color: #ccc;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 3px 8px;
            }
            QPushButton:hover {
                background-color: #555;
            }
        """)
        toolbar.addWidget(clear_btn)

        # Toggle filter-panel button (far right)
        self._filter_btn = QPushButton("☰")
        self._filter_btn.setFixedWidth(self.TOOLBAR_BUTTON_WIDTH)
        self._filter_btn.setToolTip("Show / hide entity filters")
        self._filter_btn.clicked.connect(self._toggle_filter_panel)
        self._filter_btn.setStyleSheet(self.FILTER_BUTTON_STYLE)
        toolbar.addWidget(self._filter_btn)
        toolbar.addStretch()

        # The toolbar is a dozen controls on one row, and their minimums add
        # up to something wider than the console has any need to be.  Left as
        # a plain layout that sum becomes the console's minimum width, and
        # since the console is tabbed into the dock column beneath the 2D
        # views, it set the width of that whole column -- squeezing the 3D
        # view to a slot no dock ratio could widen.  In a scroller the row
        # keeps its natural size and scrolls when there is no room for it.
        toolbar_row = QWidget()
        toolbar_row.setLayout(toolbar)
        toolbar_scroll = QScrollArea()
        toolbar_scroll.setWidget(toolbar_row)
        toolbar_scroll.setWidgetResizable(True)
        toolbar_scroll.setFrameShape(QFrame.NoFrame)
        toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        toolbar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        toolbar_scroll.setMinimumWidth(0)
        toolbar_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._toolbar_scroll = toolbar_scroll
        self._toolbar_row = toolbar_row
        # The scroll bar lives *inside* the scroll area, so its height has to
        # be allowed for or it covers the row it is scrolling.  Only when it
        # is actually needed, though: reserving the space always would leave a
        # gap under the toolbar whenever the console is wide enough.
        toolbar_scroll.horizontalScrollBar().rangeChanged.connect(
            self._fit_toolbar_height)
        self._fit_toolbar_height()
        layout.addWidget(toolbar_scroll)

        # --- Middle area: console + right-side filter column (resizable) ---
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(4)
        splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #444;
            }
            QSplitter::handle:hover {
                background-color: #F08000;
            }
        """)

        # Console text area
        self.console = LogoTextBrowser(self._logo_path, self)
        self.console.setReadOnly(True)
        # Output insertion schedules its own deferred scroll after QTextDocument
        # layout. Do not follow every range change: resizing must not move a reader.
        self.console.setFont(QFont("Fio Mono", self.font_size))
        self.console.setOpenLinks(False)
        self.console.anchorClicked.connect(self._on_anchor_clicked)
        # Toast cards: full colour when new, faint after TOAST_FADE_SECONDS,
        # full again (with a close button) while hovered or clicked.
        self._toast_seen = {}            # serial -> when first drawn
        self._toast_faint = set()        # serials drawn faint
        self._toast_highlighted = set()  # serials hovered or clicked
        self._toast_levels = {}          # serial -> level, as drawn
        #: serial -> a cursor in its card: found again in O(1) -- a cursor
        #: follows the edits around it -- and checked before it is trusted.
        self._toast_cursors = {}
        #: Runs only while a card is waiting to turn faint.
        self._toast_fader = QTimer(self)
        self._toast_fader.setInterval(250)
        self._toast_fader.timeout.connect(self._fade_toasts)
        #: serial -> (faint, close) as drawn now, each 0..1: how far the card
        #: has sunk toward faint, and how far its close button shows. A
        #: change of state eases them to the new one over
        #: TOAST_TRANSITION_SECONDS (_toast_easing), never in one jump.
        self._toast_shown = {}
        self._toast_easing = QTimer(self)
        self._toast_easing.setInterval(30)
        self._toast_easing.timeout.connect(self._ease_toasts)
        self._toast_eased_at = None
        self.console.setMouseTracking(True)
        self.console.viewport().setMouseTracking(True)
        self.console.viewport().installEventFilter(self)
        self.console.setStyleSheet("""
            QTextBrowser {
                background-color: #1a1a1a;
                color: #ddd;
                border: 1px solid #333;
                selection-background-color: #F08000;
            }
        """)
        splitter.addWidget(self.console)

        # Right-side entity-type filter column
        filter_panel = QFrame()
        filter_panel.setMinimumWidth(80)
        filter_panel.setStyleSheet("""
            QFrame {
                background-color: #252525;
                border: 1px solid #333;
                border-radius: 3px;
            }
        """)
        fp_layout = QVBoxLayout(filter_panel)
        fp_layout.setContentsMargins(6, 6, 6, 6)
        fp_layout.setSpacing(4)

        hide_label = QLabel("Hide:")
        hide_label.setStyleSheet("color: #888; font-weight: bold; border: none;")
        fp_layout.addWidget(hide_label)

        # Horizontal separator
        sep_h = QFrame()
        sep_h.setFrameShape(QFrame.HLine)
        sep_h.setStyleSheet("color: #444; border: none; max-height: 1px; background-color: #444;")
        fp_layout.addWidget(sep_h)

        cb_style = "color: #aaa; border: none;"

        self.hide_movers_cb = QCheckBox("Movers")
        self.hide_movers_cb.setToolTip("Hide I/O messages from Movers")
        self.hide_movers_cb.setStyleSheet(cb_style)
        self.hide_movers_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_movers_cb)

        self.hide_triggers_cb = QCheckBox("Triggers")
        self.hide_triggers_cb.setToolTip("Hide I/O messages from Triggers")
        self.hide_triggers_cb.setStyleSheet(cb_style)
        self.hide_triggers_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_triggers_cb)

        self.hide_doors_cb = QCheckBox("Doors")
        self.hide_doors_cb.setToolTip("Hide I/O messages from Doors")
        self.hide_doors_cb.setStyleSheet(cb_style)
        self.hide_doors_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_doors_cb)

        self.hide_monsters_cb = QCheckBox("Monsters")
        self.hide_monsters_cb.setToolTip("Hide MonsterAI debug messages")
        self.hide_monsters_cb.setStyleSheet(cb_style)
        self.hide_monsters_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_monsters_cb)

        self.hide_pathfinding_cb = QCheckBox("Pathfinding")
        self.hide_pathfinding_cb.setToolTip("Hide monster pathfinding / patrol debug messages")
        self.hide_pathfinding_cb.setStyleSheet(cb_style)
        self.hide_pathfinding_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_pathfinding_cb)

        self.hide_cutscenes_cb = QCheckBox("Cutscenes")
        self.hide_cutscenes_cb.setToolTip("Hide cutscene load / error messages")
        self.hide_cutscenes_cb.setStyleSheet(cb_style)
        self.hide_cutscenes_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_cutscenes_cb)

        self.hide_plugins_cb = QCheckBox("Plugins")
        self.hide_plugins_cb.setToolTip("Hide plugin load / error / debug messages")
        self.hide_plugins_cb.setStyleSheet(cb_style)
        self.hide_plugins_cb.toggled.connect(self._refresh_console)
        fp_layout.addWidget(self.hide_plugins_cb)

        fp_layout.addStretch()
        splitter.addWidget(filter_panel)

        # Console gets all the stretch; filter panel keeps its width
        splitter.setStretchFactor(0, 1)   # console stretches
        splitter.setStretchFactor(1, 0)   # panel stays put

        self._splitter = splitter
        self._filter_panel = filter_panel
        self._filter_panel_width = 150

        # Start collapsed
        filter_panel.hide()

        layout.addWidget(splitter, stretch=1)

        # --- Command input area at the bottom ---
        input_layout = QHBoxLayout()
        input_layout.setContentsMargins(0, 0, 0, 0)

        prompt_label = QLabel("]")
        prompt_label.setStyleSheet('color: #F08000; font-weight: bold; font-family: "Fio Mono"; font-size: 14px;')

        self.command_input = CommandInput()
        self.command_input.setPlaceholderText("Enter command...")
        self.command_input.setFont(QFont("Fio Mono", self.font_size))
        self.command_input.setStyleSheet("""
            QLineEdit {
                background-color: #1a1a1a;
                color: #ddd;
                border: 1px solid #333;
                padding: 4px;
            }
            QLineEdit:focus {
                border: 1px solid #F08000;
            }
        """)
        self.command_input.returnPressed.connect(self._on_command_entered)

        input_layout.addWidget(prompt_label)
        input_layout.addWidget(self.command_input)
        layout.addLayout(input_layout)

        # Overall widget styling
        self.setStyleSheet("""
            QWidget {
                background-color: #2b2b2b;
            }
            QComboBox {
                background-color: #3a3a3a;
                color: #ddd;
                border: 1px solid #555;
                padding: 3px;
                border-radius: 3px;
            }
            QComboBox:hover {
                border-color: #F08000;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #3a3a3a;
                color: #ddd;
                selection-background-color: #F08000;
            }
        """)

    # ------------------------------------------------------------------
    # Font size controls
    # ------------------------------------------------------------------

    def _increase_font_size(self):
        """Increase console font size by 1pt and refresh."""
        if self.font_size < self.FONT_SIZE_MAX:
            self.font_size += 1
            self._apply_font_size()

    def _decrease_font_size(self):
        """Decrease console font size by 1pt and refresh."""
        if self.font_size > self.FONT_SIZE_MIN:
            self.font_size -= 1
            self._apply_font_size()

    def _show_font_size(self):
        """The font size, in the size buttons' tooltips."""
        self._decrease_btn.setToolTip(f"Decrease font size ({self.font_size} pt)")
        self._increase_btn.setToolTip(f"Increase font size ({self.font_size} pt)")

    def _show_message_count(self):
        """The message count, in the Clear button's tooltip."""
        self._clear_btn.setToolTip(f"Clear {self.message_count} messages")

    def _apply_font_size(self):
        """Apply the current font size to the console and reload all HTML."""
        self._show_font_size()
        self.console.setFont(QFont("Fio Mono", self.font_size))
        self.command_input.setFont(QFont("Fio Mono", self.font_size))
        self._refresh_console()

    # ------------------------------------------------------------------

    def _fit_toolbar_height(self, *_range):
        """Make room for the toolbar's scroll bar only when there is one."""
        bar = self._toolbar_scroll.horizontalScrollBar()
        extra = bar.sizeHint().height() if bar.maximum() > 0 else 0
        self._toolbar_scroll.setFixedHeight(
            self._toolbar_row.sizeHint().height() + extra + 2)

    def _connect_logger(self):
        """Connect to the global debug logger."""
        logger = get_debug_logger()
        logger.message_logged.connect(self._on_message)
        logger.toast_repeated.connect(self._on_toast_repeated)

    def _load_buffer(self):
        """Load any buffered messages that were logged before the console opened."""
        logger = get_debug_logger()
        lines = [self._format_message(category, message)
                 for category, message in logger.get_buffer()]
        self._insert_lines([line for line in lines if line is not None])

    def _on_message(self, category: str, message: str):
        """Show a message: at once when the console is idle, batched in a burst.

        An isolated line -- a command's reply, a load message -- appears
        immediately and opens a short window; lines arriving inside it are
        queued and inserted together when it closes, and a flush that finds
        more queued keeps the window open for as long as the burst lasts.
        """
        if sip.isdeleted(self._flush_timer):
            # The console is being torn down: its timer (a child) is gone
            # before the logger connection is, and there is nowhere to write.
            return
        if self._flush_timer.isActive():
            self._pending.append((category, message))
            return
        self._append_message(category, message)
        self._flush_timer.start()

    def _on_command_entered(self):
        """Handle command submission from the input line."""
        cmd = self.command_input.text().strip()
        if not cmd:
            return

        # Echo the command to the console exactly like Quake
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(_ECHO_HTML.format(html.escape(cmd, quote=False)))
        get_debug_logger().log_command_echo(cmd)

        # Add to local history and clear the line
        self.command_input.add_history(cmd)
        self.command_input.clear()

        # Keep the newest command visible.
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

        # Pass the raw string to whatever is listening
        self.command_issued.emit(cmd)

    # -- toast cards ---------------------------------------------------------

    def _toast_state(self, serial):
        if serial in self._toast_highlighted:
            return 'highlighted'
        return 'faint' if serial in self._toast_faint else 'new'

    #: Each state's (faint, close) amounts.
    _TOAST_TARGETS = {'new': (0.0, 0.0), 'faint': (1.0, 0.0), 'highlighted': (0.0, 1.0)}

    @staticmethod
    def _toast_colours(level, state, amounts=None):
        """``(accent, text, body, close button)`` colours of a card, for
        *state*, or for *amounts* ``(faint, close)`` part way between."""
        border, ink, body, _icon = TOAST_STYLES.get(level, TOAST_STYLES['info'])
        faint, shown = (amounts if amounts is not None
                        else DebugConsole._TOAST_TARGETS.get(state, (0.0, 0.0)))
        if faint > 0.0:
            border = _mix(border, _CONSOLE_BG, TOAST_FAINT * faint).name()
            ink = _mix(ink, _CONSOLE_BG, TOAST_FAINT * faint).name()
            body = _mix(body, _CONSOLE_BG, TOAST_FAINT * faint).name()
        close = _mix(body, ink, shown).name() if shown > 0.0 else body
        return border, ink, body, close

    def _toast_paragraphs(self, serial, level, text, ink, close_ink):
        """The words of toast *serial*'s card as HTML paragraphs: its text, a
        ``×N`` after the first line when it was logged N times, an error's
        copy button and the close button."""
        # One paragraph a line: with <br> between them, Qt sized the box
        # as if all the lines were one, far wider than the longest.
        # Leading spaces (an indented list) are kept.
        def _line(line):
            stripped = line.lstrip(' ')
            return ('&nbsp;' * (len(line) - len(stripped))
                    + html.escape(stripped, quote=False))
        rows = [_line(line) for line in text.split('\n')]
        count = get_debug_logger().toast_count(serial)
        if count > 1:
            rows[0] += f'&nbsp;&nbsp;<b>\u00d7{count}</b>'
        # Only an error has the copy button (to put it on the clipboard);
        # a success, warning or note is just its words.
        copy = (f'&nbsp;&nbsp;<a href="{_TOAST_COPY_LINK}{serial}" '
                f'title="Copy the error">'
                f'<img src="{_TOAST_COPY_IMAGE}" width="16" height="16" '
                f'style="vertical-align: middle;"></a>'
                if level == 'error' else '')
        # The close button: tiny, and shown (in the text's colour) only
        # while the card is highlighted -- otherwise drawn in the card's
        # own colour, and a click on it does nothing.
        close = (f'&nbsp;&nbsp;<a href="{_TOAST_CLOSE_LINK}{serial}" '
                 f'title="Remove this toast" style="color: {close_ink}; '
                 f'text-decoration: none; font-size: small;">\u2715</a>')
        return ''.join(
            f'<p style="margin: 0; color: {ink};">'
            + (f'<a name="fio-toast-{serial}"></a>' if i == 0 else '')
            + row
            + (copy + close if i == len(rows) - 1 else '') + '</p>'
            for i, row in enumerate(rows))

    def _on_toast_repeated(self, serial, count):
        """Toast *serial* was logged again: its card shows the count and is
        in full colour again (its fade starts over). A card not drawn yet
        (still queued) shows the count when it is."""
        import time
        from PyQt5.QtGui import QTextCursor
        serial = str(serial)
        self._toast_seen[serial] = time.monotonic()
        self._toast_faint.discard(serial)
        table = self._toast_table(serial)
        if table is None:
            return
        text = get_debug_logger().toast_text(serial)
        if text is None:
            return
        level = self._toast_levels.get(serial, 'info')
        state = self._toast_state(serial)
        _border, ink, _body, close_ink = self._toast_colours(level, state)
        self._toast_shown[serial] = self._TOAST_TARGETS[state]
        edit = QTextCursor(self.console.document())
        edit.beginEditBlock()
        try:
            # The card's words again, with the count: inside its cell, so
            # the card itself (and everything around it) stays as it is.
            cell = table.cellAt(0, 1)
            words = cell.firstCursorPosition()
            words.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
            words.insertHtml(self._toast_paragraphs(
                serial, level, text.translate(_MARKUP_CHARS), ink, close_ink))
            # Logged again: back in full colour now, words and card alike.
            self._paint_toast(serial, self._toast_shown[serial])
        finally:
            edit.endEditBlock()
        self._toast_cursors.pop(serial, None)
        self._wake_fader()

    def _toast_table(self, serial):
        """The console document's table drawing toast *serial*, or None.

        From the cursor kept in its card when there is one and it still
        points into that card; else every card is found again in one walk
        of the document (after a rebuild, say) and their cursors kept, so a
        burst of cards fading together costs one walk, not one each."""
        serial = str(serial)
        cursor = self._toast_cursors.get(serial)
        if cursor is not None:
            table = cursor.currentTable()
            if table is not None and self._toast_serial_of(table) == serial:
                return table
        from PyQt5.QtGui import QTextCursor, QTextTable
        self._toast_cursors = {}
        found = None
        for frame in self.console.document().rootFrame().childFrames():
            if not isinstance(frame, QTextTable):
                continue
            number = self._toast_serial_of(frame)
            if number is None:
                continue
            self._toast_cursors[number] = QTextCursor(frame.cellAt(0, 1).firstCursorPosition())
            if number == serial:
                found = frame
        return found

    def _toast_serial_of(self, table):
        if table is None or table.columns() < 2:
            return None
        block = table.cellAt(0, 1).firstCursorPosition().block()
        it = block.begin()
        while not it.atEnd():
            for name in it.fragment().charFormat().anchorNames():
                if name.startswith("fio-toast-"):
                    return name[len("fio-toast-"):]
            it += 1
        return None

    def _restyle_toast(self, serial):
        """Ease toast *serial*'s card to its state's colours, over
        TOAST_TRANSITION_SECONDS (a card not drawn yet takes them at once)."""
        target = self._TOAST_TARGETS[self._toast_state(serial)]
        if self._toast_shown.get(serial) is None:
            self._toast_shown[serial] = target
            self._paint_toast(serial, target)
            return
        if self._toast_shown[serial] != target and not self._toast_easing.isActive():
            import time
            self._toast_eased_at = time.monotonic()
            self._toast_easing.start()

    def _ease_toasts(self):
        """One step of every card easing toward its state's colours; stop
        when none is left between two states."""
        import time
        from PyQt5.QtGui import QTextCursor
        now = time.monotonic()
        step = (now - (self._toast_eased_at or now)) / TOAST_TRANSITION_SECONDS
        self._toast_eased_at = now
        moving = False
        edit = QTextCursor(self.console.document())
        edit.beginEditBlock()           # every card that moved: one layout
        try:
            for serial, shown in list(self._toast_shown.items()):
                target = self._TOAST_TARGETS[self._toast_state(serial)]
                if shown == target:
                    continue
                eased = tuple(
                    t if abs(t - v) <= step else v + step * (1.0 if t > v else -1.0)
                    for v, t in zip(shown, target))
                self._toast_shown[serial] = eased
                self._paint_toast(serial, eased)
                moving = moving or eased != target
        finally:
            edit.endEditBlock()
        if not moving:
            self._toast_easing.stop()

    def _paint_toast(self, serial, amounts):
        """Recolour toast *serial*'s card in place, at *amounts*."""
        from PyQt5.QtGui import QColor, QTextCharFormat, QTextCursor
        table = self._toast_table(serial)
        if table is None:
            return
        level = self._toast_levels.get(serial)
        if level is None:
            level = next((lv for sr, lv, _t in get_debug_logger().toasts() if sr == serial),
                         'info')
        border, ink, body, close = self._toast_colours(level, None, amounts)
        for column, colour in ((0, border), (1, body)):
            cell = table.cellAt(0, column)
            fmt = cell.format()
            fmt.setBackground(QColor(colour))
            cell.setFormat(fmt)
        cell = table.cellAt(0, 1)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.KeepAnchor)
        text_fmt = QTextCharFormat()
        text_fmt.setForeground(QColor(ink))
        cursor.mergeCharFormat(text_fmt)
        # The close button: the cell's last character, the tiny cross.
        end = cell.lastCursorPosition()
        mark = QTextCursor(end)
        mark.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor)
        if mark.selectedText() == '\u2715':
            close_fmt = QTextCharFormat()
            close_fmt.setForeground(QColor(close))
            mark.mergeCharFormat(close_fmt)

    def _fade_toasts(self):
        """Turn faint the cards that have been in full colour long enough;
        stop the timer when no card is waiting."""
        import time
        now = time.monotonic()
        waiting = False
        due = []
        for serial, since in list(self._toast_seen.items()):
            if serial in self._toast_faint or serial in self._toast_highlighted:
                continue
            if now - since < TOAST_FADE_SECONDS:
                waiting = True
                continue
            due.append(serial)
        if due:
            # One edit: the document is laid out again once, from the first
            # card changed, not once a card (each from there to the end).
            from PyQt5.QtGui import QTextCursor
            edit = QTextCursor(self.console.document())
            edit.beginEditBlock()
            try:
                for serial in due:
                    self._toast_faint.add(serial)
                    self._restyle_toast(serial)
            finally:
                edit.endEditBlock()
        if not waiting:
            self._toast_fader.stop()

    def _wake_fader(self):
        """A card is in full colour and will turn faint: tick until it has."""
        if not self._toast_fader.isActive():
            self._toast_fader.start()

    def _highlight_toast(self, serial):
        """Full colour and a close button for *serial*; the card that was
        highlighted fades again in TOAST_FADE_SECONDS."""
        import time
        from PyQt5.QtGui import QTextCursor
        others = [other for other in self._toast_highlighted if other != serial]
        new = serial is not None and serial not in self._toast_highlighted
        if not (others or new):
            return                      # the same card, or none: nothing to redraw
        edit = QTextCursor(self.console.document())
        edit.beginEditBlock()           # both cards: one layout
        try:
            for other in others:
                self._toast_highlighted.discard(other)
                self._toast_seen[other] = time.monotonic()
                self._toast_faint.discard(other)
                self._restyle_toast(other)
                self._wake_fader()
            if new:
                self._toast_highlighted.add(serial)
                self._toast_faint.discard(serial)
                self._restyle_toast(serial)
        finally:
            edit.endEditBlock()

    def remove_toast(self, serial):
        """Remove toast *serial*'s card from the console and its history."""
        from PyQt5.QtGui import QTextCursor
        serial = str(serial)
        table = self._toast_table(serial)
        if table is not None:
            cursor = QTextCursor(self.console.document())
            cursor.setPosition(table.firstPosition() - 1)
            cursor.setPosition(table.lastPosition() + 1, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        get_debug_logger().remove_toast(serial)
        self._toast_seen.pop(serial, None)
        self._toast_levels.pop(serial, None)
        self._toast_cursors.pop(serial, None)
        self._toast_faint.discard(serial)
        self._toast_highlighted.discard(serial)
        self._toast_shown.pop(serial, None)

    def eventFilter(self, obj, event):
        if obj is self.console.viewport():
            kind = event.type()
            if kind in (QEvent.MouseMove, QEvent.MouseButtonPress):
                table = self.console.cursorForPosition(event.pos()).currentTable()
                serial = self._toast_serial_of(table)
                if serial is not None or self._toast_highlighted:
                    self._highlight_toast(serial)
            elif kind == QEvent.Leave and self._toast_highlighted:
                self._highlight_toast(None)
        return super().eventFilter(obj, event)

    def _on_anchor_clicked(self, url: QUrl):
        """Handle clicking on an entity filter or an external URL."""
        link = url.toString()
        if link.startswith(_TOAST_CLOSE_LINK):
            # A toast's close button: only while it shows (highlighted).
            serial = link[len(_TOAST_CLOSE_LINK):]
            if serial in self._toast_highlighted:
                self.remove_toast(serial)
        elif link.startswith(_TOAST_COPY_LINK):
            # A console toast's copy button: its whole message.
            text = get_debug_logger().toast_text(link[len(_TOAST_COPY_LINK):])
            if text is not None:
                QApplication.clipboard().setText(text)
        elif link.startswith("filter:"):
            entity_name = link.split(":", 1)[1]
            self._apply_entity_filter(entity_name)
        elif link.startswith(("http://", "https://")):
            QDesktopServices.openUrl(url)

    def _apply_entity_filter(self, entity_name):
        """Updates the dropdown to filter by this entity."""

        # If we clicked the same entity that is currently filtered, toggle it off (Reset to All)
        if self.active_entity_filter == entity_name:
            self.filter_combo.setCurrentText("All")
            return

        filter_text = f"Entity: {entity_name}"

        # Check if this item already exists in combo, if not add it
        idx = self.filter_combo.findText(filter_text)
        if idx == -1:
            self.filter_combo.addItem(filter_text)
            idx = self.filter_combo.count() - 1

        # Select it (this will trigger _on_filter_changed -> _refresh_console)
        self.filter_combo.setCurrentIndex(idx)

    def _on_filter_changed(self, text):
        """Handle filter dropdown changes."""

        # Clean up old entity filters if we switched away from them
        count = self.filter_combo.count()
        # Iterate backwards to safely remove
        for i in range(count - 1, -1, -1):
            item_text = self.filter_combo.itemText(i)
            # If it's an entity filter...
            if item_text.startswith("Entity: "):
                # And it's NOT the one we just selected...
                if item_text != text:
                    self.filter_combo.removeItem(i)

        if text.startswith("Entity: "):
            self.active_entity_filter = text.split("Entity: ", 1)[1]
        else:
            self.active_entity_filter = None

        self._refresh_console()

    #: Messages logged from the logic and AI threads arrive as queued signals,
    #: hundreds a second in a monster fight with I/O logging on. Formatting
    #: and inserting each one as it came could not keep up -- the GUI thread
    #: fell further behind every frame and the editor froze -- so they are
    #: queued and inserted as one block per interval.
    FLUSH_INTERVAL_MS = 100
    #: Most lines one flush inserts; a burst beyond it shows its newest lines
    #: and says how many it skipped (the log buffer still holds them).
    MAX_LINES_PER_FLUSH = 200
    #: The document is rebuilt from the log buffer past this many lines, so
    #: it cannot grow without bound during a long session.
    MAX_DOCUMENT_LINES = 5000

    def _append_message(self, category: str, message: str):
        """Append a message to the console with highlighting, now."""
        html = self._format_message(category, message)
        if html is not None:
            self._insert_lines([html])

    def _scroll_to_latest(self):
        """Keep the console at the scrollbar's real maximum after output layout."""
        scrollbar = self.console.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _insert_lines(self, lines):
        """Insert formatted lines at the end in one edit, and scroll once."""
        if not lines:
            return
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertHtml(''.join(lines))
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()
        self._scroll_to_latest()
        # QTextDocument may settle its layout and scrollbar range after this
        # insertion returns. Defer only this output-triggered scroll; arbitrary
        # range changes (such as resizing) must preserve the reader's position.
        QTimer.singleShot(0, self._scroll_to_latest)
        self.message_count += len(lines)
        self._document_lines += len(lines)
        self._show_message_count()
        if self._document_lines > self.MAX_DOCUMENT_LINES:
            self._refresh_console()

    def _flush_pending(self):
        """Format and insert every message queued since the last flush."""
        pending = self._pending
        if not pending:
            return                       # the burst is over; next line is immediate
        self._pending = []
        self._flush_timer.start()        # still bursting: keep batching
        skipped = max(0, len(pending) - self.MAX_LINES_PER_FLUSH)
        lines = []
        if skipped:
            pending = pending[skipped:]
            lines.append('<span style="color: #888888;">&#8230; %d messages '
                         'not shown (still in the log buffer)</span><br>'
                         % skipped)
        for category, message in pending:
            html = self._format_message(category, message)
            if html is not None:
                lines.append(html)
        self._insert_lines(lines)

    def _format_message(self, category: str, message: str):
        """One message as a highlighted HTML line, or ``None`` if filtered out.

        A message is plain text: every ``<``, ``>`` and ``&`` in it is shown
        literally, so a map-controlled string (an entity name, a command)
        cannot place an image, a link or any other tag in the console. Only
        a line logged with :func:`debug_log_markup` keeps its own tags.
        """
        message = str(message)
        if category == ECHO_CATEGORY:
            # A typed command, drawn as it was when entered (whatever the
            # filters: the live echo ignores them too).
            return _ECHO_HTML.format(html.escape(message[len(_ECHO_PREFIX):], quote=False))
        if message.startswith(_TOAST):
            # A toast: a card in its colour -- its words (an error's with a
            # copy button), no icon in front -- and
            # no links in its words. No filter hides a toast: not the
            # category, an entity, nor any of the hide boxes.
            level, serial, text = _toast_parts(message)
            text = text.translate(_MARKUP_CHARS)
            self._toast_seen.setdefault(serial, __import__('time').monotonic())
            self._toast_levels[serial] = level
            if serial not in self._toast_faint and serial not in self._toast_highlighted:
                self._wake_fader()
            state = self._toast_state(serial)
            border, ink, body, close_ink = self._toast_colours(level, state)
            self._toast_shown[serial] = self._TOAST_TARGETS[state]
            document = self.console.document()
            from PyQt5.QtGui import QTextDocument
            if document.resource(QTextDocument.ImageResource, QUrl(_TOAST_COPY_IMAGE)) is None:
                document.addResource(QTextDocument.ImageResource,
                                     QUrl(_TOAST_COPY_IMAGE), _toast_copy_image())
            paragraphs = self._toast_paragraphs(serial, level, text, ink, close_ink)
            # A card, as the pop-up toasts are: a tinted body with an accent
            # bar down its left side. (A cell takes a background; Qt ignores
            # a table's.)
            return (
                f'<table cellspacing="0" cellpadding="0" border="0" '
                f'style="margin-top: 2px; margin-bottom: 2px;"><tr>'
                f'<td width="3" bgcolor="{border}"></td>'
                f'<td bgcolor="{body}" style="padding: 2px 8px 2px 7px;">'
                f'{paragraphs}</td></tr></table>')
        is_markup = message.startswith(_MARKUP)
        no_links = not is_markup and message.startswith(_NO_LINKS)
        if no_links:
            message = message[len(_NO_LINKS):]
        # The reserved characters mean nothing anywhere else in a line.
        message = (message[len(_MARKUP):] if is_markup else message).translate(_MARKUP_CHARS)

        # 1. Check Category Filter vs Entity Filter
        current_combo_text = self.filter_combo.currentText()
        # Raw console lines still behave as Info for category filtering,
        # but deliberately retain their unprefixed display form.
        filter_category = category or 'Info'

        # If we are in "Entity: X" mode
        if self.active_entity_filter:
            # We filter OUT messages that don't contain the entity name
            # Simple substring check is usually sufficient for debug logs
            if self.active_entity_filter not in message:
                return
        # If we are in standard Category mode (and not "All")
        elif current_combo_text != "All" and filter_category != current_combo_text:
            return

        # 2. Filter specific entity types (Movers, Triggers, Doors)
        if self.hide_movers_cb.isChecked() or self.hide_triggers_cb.isChecked() or self.hide_doors_cb.isChecked():
            match = self._RE_IO_ENTITY.match(message)
            if match:
                entity_name = match.group(1)
                if self.hide_movers_cb.isChecked() and entity_name.startswith('Mover'):
                    return
                if self.hide_triggers_cb.isChecked() and entity_name.startswith('Trigger'):
                    return
                if self.hide_doors_cb.isChecked() and entity_name.startswith('Door'):
                    return
            # Also filter lines with no entity name (likely unnamed triggers)
            if message.startswith('[IO] .'):
                if self.hide_triggers_cb.isChecked():
                    return

        # 2b. Filter MonsterAI messages when "Monsters" checkbox is checked
        if self.hide_monsters_cb.isChecked() and category == 'MonsterAI':
            return

        # 2c. Filter Pathfinding messages when "Pathfinding" checkbox is checked
        if self.hide_pathfinding_cb.isChecked() and category == 'Pathfinding':
            return

        # 2d. Filter Plugins messages when "Plugins" checkbox is checked
        if self.hide_cutscenes_cb.isChecked() and category == 'Cutscene':
            return

        if self.hide_plugins_cb.isChecked() and category == 'Plugins':
            return

        # 3. Check "Filter Empty" Logic
        if self.filter_empty_cb.isChecked():
            if "(0 connections)" in message:
                return
            if "(no connections for output" in message:
                return

        # Get color for category
        color = self.CATEGORY_COLORS.get(filter_category, '#FFFFFF')

        # Plugin messages all share one category; recolour by content so
        # loads read green, errors red, and debug/init stay blue.
        if filter_category == 'Plugins':
            color = self._plugin_message_color(message)
        elif filter_category == 'Cutscene':
            color = (
                self.CUTSCENE_ERROR_COLOR
                if message.upper().startswith('[CUTSCENE] FAILED:')
                else self.CUTSCENE_LOAD_COLOR
            )

        # --- HIGHLIGHTING LOGIC ---

        # Render the startup version banner in one dedicated pass. Keeping its
        # source text plain prevents the generic entity highlighter from
        # rewriting its own filter anchors.
        version_prefix = "[Info] Fio version "
        if message.startswith(version_prefix):
            version_text = message[len(version_prefix):].strip()
            # The startup log currently says "Fio version version X.Y.Z.B".
            # Treat both that form and the older "Fio version X.Y.Z.B" form
            # as a single presentation-only banner so the generic entity
            # highlighter cannot turn 2, 5, or 10 into filter links.
            version_match = re.fullmatch(
                r"(?:version\s+)?(\d+\.\d+\.\d+\.\d+)",
                version_text,
            )
            if version_match:
                version_html = self._version_banner_html(version_match.group(1))
                return f'<span style="color: {color};">{version_html}</span><br>'

        # A toast written to the console: plain words, none of them a
        # click-to-filter link -- not its numbers, names or values.
        if no_links:
            return (f'<span style="color: {color};">'
                    f'{html.escape(message, quote=False)}</span><br>')

        # The startup OpenGL line ("OpenGL 4.1 Metal - 90.5 | Apple M5 | ...")
        # is hardware text: its version numbers are not entities, so nothing
        # in it becomes a filter link. This line only.
        if message.startswith("[Info] OpenGL "):
            return (f'<span style="color: {color};">'
                    f'{html.escape(message, quote=False)}</span><br>')

        # Placeholders: NUL, which no message can contain (removed above),
        # around private-use characters, which no highlighting regex matches.
        def _placeholder(kind, index):
            base = 0xE000 if kind == "H" else 0xE100
            return '\x00' + ''.join(chr(base + int(d)) for d in str(index)) + '\x00'
        _protected_tags = []
        if is_markup:
            # Fio's own markup (debug_log_markup): keep its tags out of the
            # way of the highlighting regexes below.
            def _protect_tag(m):
                _protected_tags.append(m.group(0))
                return _placeholder("H", len(_protected_tags) - 1)
            _msg_temp = re.sub(r'</?[a-zA-Z][^>]*>', _protect_tag, message)
        elif filter_category in _INERT_MARKUP_CATEGORIES:
            # Older internal lines (MonsterAI's coloured names, the help
            # text) carry inert formatting; everything else is escaped.
            def _protect_inert(tag):
                _protected_tags.append(tag)
                return _placeholder("H", len(_protected_tags) - 1)
            _msg_temp = _inert_markup(message, _protect_inert)
        else:
            # Plain text, possibly from a map: nothing in it is a tag.
            _msg_temp = html.escape(message, quote=False)

        # Define styles
        ENT_STYLE = 'color: #F08000; font-weight: bold; text-decoration: none;'
        FIRE_STYLE = 'color: #66BB6A; font-weight: bold;'
        EMPTY_STYLE = 'color: #E35335;'

        def get_link_html(name):
            return f'<a href="filter:{name}" style="{ENT_STYLE}" title="Click to filter by {name}">{name}</a>'

        def get_external_link_html(url):
            # The matched text is already escaped; make it attribute-safe.
            url = html.escape(html.unescape(url), quote=True)
            title = ('Open Fio on GitHub'
                     if url.startswith('https://github.com/vicioussquid/Fio')
                     else 'Open this link in your browser')
            return (
                f'<a href="{url}" '
                f'style="color: #26A69A; text-decoration: underline;" '
                f'title="{title}">{url}</a>'
            )

        # A raw startup URL is a real clickable link, not an entity filter.
        # Protect the generated anchor too, so the normal entity pass cannot
        # rewrite "github.com" as an entity link.
        _external_links = []
        def _protect_external_link(m):
            _external_links.append(
                get_external_link_html(
                    m.group(0) if m.group(0).startswith('http') else
                    'https://' + m.group(0))
            )
            return _placeholder("L", len(_external_links) - 1)

        # A URL ends at white space, a tag, a quote or an escaped < or >.
        _url_tail = r'(?:(?!&lt;|&gt;)[^\s<>"\'\x00])+'
        _msg_temp = re.sub(
            r'(?<![\w/])https?://' + _url_tail + r'|(?<![\w/])github\.com/' + _url_tail,
            _protect_external_link,
            _msg_temp
        )

        # Apply Regex substitutions to _msg_temp (protected string)

        # A. Entity Names: "Name.Input"
        _msg_temp = self._RE_ENTITY_DOT.sub(
            lambda m: get_link_html(m.group(1)),
            _msg_temp
        )

        # B. Entity Names: "Name (type=...)"
        _msg_temp = self._RE_ENTITY_TYPE.sub(
            lambda m: get_link_html(m.group(1)),
            _msg_temp
        )

        # C. The built-in "help" command is presentation-only here; keep
        # its bold orange styling but do not make it an entity filter link.
        _msg_temp = self._RE_HELP_QUOTE.sub(
            lambda m: f"{m.group(1)}<span style=\"{ENT_STYLE}\">help</span>{m.group(1)}",
            _msg_temp
        )

        # D. Entity Names: "'Name'"
        _msg_temp = self._RE_ENTITY_QUOTE.sub(
            lambda m: f"'{get_link_html(m.group(1))}'", 
            _msg_temp
        )

        # E. "fire_output" -> Green
        _msg_temp = self._RE_FIRE_OUTPUT.sub(
            f'<span style="{FIRE_STYLE}">\1</span>',
            _msg_temp
        )

        # F. "no connections" -> Red/Orange
        _msg_temp = self._RE_NO_CONNS.sub(
            f'<span style="{EMPTY_STYLE}">\1</span>',
            _msg_temp
        )

        # G. Style [Delayed] prefix (orange)
        _msg_temp = self._RE_DELAYED.sub('<span style="color: #FFB74D;">[Delayed]</span>', _msg_temp)

        # H. Style arrow -> as green arrow character
        _msg_temp = self._RE_ARROW.sub(' <span style="color: #66BB6A;">→</span> ', _msg_temp)

        # Restore protected HTML tags and generated external anchors.
        for _i, _tag in enumerate(_protected_tags):
            _msg_temp = _msg_temp.replace(_placeholder("H", _i), _tag)
        for _i, _tag in enumerate(_external_links):
            _msg_temp = _msg_temp.replace(_placeholder("L", _i), _tag)
        message = _msg_temp

        # ---------------------------

        # Format with HTML coloring for the main message body
        return f'<span style="color: {color};">{message}</span><br>'

    def _version_banner_html(self, version: str) -> str:
        """Render the startup version banner without entity-filter links."""
        parts = version.split('.')
        if len(parts) != 4:
            return f'<b>Fio version</b> <b>{version}</b>'

        # The banner itself is white. Only major/minor/patch are orange;
        # dots, the build number, and the surrounding label remain white.
        white_style = 'color: #FFFFFF; font-weight: bold;'
        version_style = 'color: #F08000; font-weight: bold;'

        rendered = [f'<span style="{white_style}">Fio version</span> ']
        for index, part in enumerate(parts):
            style = version_style if index < 3 else white_style
            rendered.append(f'<span style="{style}">{part}</span>')
            if index < len(parts) - 1:
                rendered.append(f'<span style="{white_style}">.</span>')
        return ''.join(rendered)
    def _plugin_message_color(self, message: str) -> str:
        """Pick a colour for a 'Plugins' message based on its content.

        Failures are red, successful loads green, and anything else (debug /
        init chatter) falls back to the blue default. Errors win over loads so
        a message like "register() failed" reads red rather than green.
        """
        lowered = message.lower()
        if any(hint in lowered for hint in self._PLUGIN_ERROR_HINTS):
            return self.PLUGIN_ERROR_COLOR
        if any(hint in lowered for hint in self._PLUGIN_LOAD_HINTS):
            return self.PLUGIN_LOAD_COLOR
        return self.PLUGIN_DEBUG_COLOR

    def _refresh_console(self):
        """Reload console messages from buffer (triggered by filters or font size change)."""
        self.console.clear()
        self.message_count = 0
        self._document_lines = 0
        # Anything still queued is already in the buffer being reloaded.
        self._pending = []

        logger = get_debug_logger()
        lines = [self._format_message(category, message)
                 for category, message in logger.get_buffer()]
        self._insert_lines([line for line in lines if line is not None])

    def clear(self):
        """Clear the console and buffer."""
        self._toast_seen.clear()
        self._toast_faint.clear()
        self._toast_highlighted.clear()
        self._toast_levels.clear()
        self._toast_cursors.clear()
        self._toast_fader.stop()
        self._toast_shown.clear()
        self._toast_easing.stop()
        self.console.clear()
        self.message_count = 0
        self._document_lines = 0
        self._pending = []
        self._show_message_count()
        get_debug_logger().clear_buffer()

    def toggle(self):
        """Toggle visibility of the console."""
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _toggle_filter_panel(self):
        """Show or hide the right-side filter column."""
        if self._filter_panel.isVisible():
            self._filter_panel.hide()
            self._filter_btn.setStyleSheet(self.FILTER_BUTTON_STYLE)
        else:
            self._filter_panel.show()
            pw = self._filter_panel_width
            self._splitter.setSizes([self.width() - pw, pw])
            self._filter_btn.setStyleSheet(self.FILTER_BUTTON_OPEN_STYLE)

    def closeEvent(self, event):
        """Handle close - just hide instead of destroying."""
        self.hide()
        event.ignore()
