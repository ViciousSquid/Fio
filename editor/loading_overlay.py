"""
A "still working" panel for the long, blocking stretches of the editor.

Opening a big map parses tens of megabytes of JSON, rebuilds the terrain and
uploads the scene to the GPU, and starting play builds the world's streaming
and physics; all of it runs on the UI thread. For those seconds the window
used to sit frozen on whatever it showed last, which looks exactly like a
crash.

:class:`LoadingOverlay` is a small panel floating in the middle of the
window -- the rest of the editor stays visible behind it -- with a title
("Loading cave01"), a throbber (a short horizontal line travelling back and
forth along a track) and the stage under way ("Building terrain"). It used
to be a veil over the whole window with a percentage bar, which for a load
of a second or two was a full-screen flash.

Work on the UI thread cannot be interrupted to animate anything, so the long
operations call :meth:`step` between their stages: each call names the stage
and repaints the panel at once (``QWidget.repaint`` paints and flushes
without running the event loop, so nothing else, such as a half-loaded frame
of the 3D view, gets a chance to run). The throbber's line is placed by the
clock, so each repaint shows it where the time has carried it; no timer, no
thread and no second process draws it.

Use it as a context manager; nesting is counted, so an operation built from
others (a level change = load its map + restart play) shows one panel
throughout::

    with window.loading_overlay.busy("Loading cave01", "Reading the map", 5):
        ...
        window.loading_overlay.step("Building terrain", 40)

The percentages are still taken (and only ever move forward, see
:attr:`LoadingOverlay.value`) so callers and tools can tell how far a load
has got; the panel does not show them.
"""

from __future__ import annotations

import contextlib
import time

from PyQt5.QtCore import QEvent, QObject, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QWidget

#: Where a stage named without a percentage puts the progress: this share of
#: the way from where it is to the end, so it always moves and never fills.
_ADVANCE = 0.25
_CEILING = 95

#: The height of the startup splash's progress bar (``main.py``).
BAR_HEIGHT = 10

#: The floating panel, in logical pixels.
PANEL_WIDTH = 320
PANEL_HEIGHT = 92
#: The throbber: a track with a line this share of its width, crossing it
#: and coming back once every THROB_SECONDS.
THROB_LINE = 0.28
THROB_SECONDS = 1.6
FIO_ORANGE = QColor(240, 128, 0)


def throbber_span(seconds: float, width: float):
    """``(start, end)`` in pixels of the throbber's line on a track *width*
    wide, *seconds* after it started: left to right and back, eased at the
    turns."""
    phase = (seconds / THROB_SECONDS) % 1.0
    travel = 1.0 - abs(2.0 * phase - 1.0)            # 0 -> 1 -> 0
    travel = travel * travel * (3.0 - 2.0 * travel)  # smoothstep
    line = width * THROB_LINE
    start = (width - line) * travel
    return start, start + line


class LoadingOverlay(QWidget):
    """A small floating "loading" panel with a throbber; see the module docstring."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.title = ""
        self.stage = ""
        self._depth = 0
        self._value = 0
        self._started = 0.0
        #: Injected by tests: the clock the throbber is placed by.
        self.clock = time.perf_counter
        parent.installEventFilter(self)
        self.hide()

    # -------------------------------------------------------------- lifecycle
    @property
    def active(self) -> bool:
        return self._depth > 0

    @property
    def value(self) -> int:
        """How far the operation has got, 0-95; not drawn."""
        return self._value

    def begin(self, title: str, stage: str = "", percent: int = None) -> None:
        """Raise the panel (or, nested, just move on to *stage*)."""
        self._depth += 1
        if self._depth == 1:
            self.title = str(title)
            self._value = 0
            self._started = self.clock()
            self._layout()
            self.show()
            self.raise_()
        self.step(stage or "", percent)

    def end(self) -> None:
        """Lower the panel once the outermost operation is done."""
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self.hide()

    @contextlib.contextmanager
    def busy(self, title: str, stage: str = "", percent: int = None):
        self.begin(title, stage, percent)
        try:
            yield self
        finally:
            self.end()

    def step(self, stage: str, percent: int = None) -> None:
        """Name the stage now under way and show it, and the throbber, at once."""
        if not self.active:
            return
        if stage:
            self.stage = str(stage)
        current = self._value
        if percent is None:
            target = current + max(1, int((_CEILING - current) * _ADVANCE))
        else:
            target = int(percent)
        self._value = max(current, min(_CEILING, target))
        try:
            self.raise_()
            self.repaint()
        except RuntimeError:
            pass   # the window is being torn down

    def _layout(self):
        """Centre the panel in the window; it covers nothing else."""
        parent = self.parentWidget()
        w = min(PANEL_WIDTH, max(160, parent.width() - 24))
        h = min(PANEL_HEIGHT, max(60, parent.height() - 24))
        self.setGeometry((parent.width() - w) // 2, (parent.height() - h) // 2, w, h)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._layout()
        return False

    # ------------------------------------------------------------------ paint
    def throbber_track(self) -> QRectF:
        """The throbber's track within the panel."""
        margin = 22.0
        return QRectF(margin, self.height() / 2.0 + 2.0, self.width() - 2 * margin, 2.0)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        panel = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor(85, 85, 90), 1.0))
        p.setBrush(QColor(24, 24, 28, 245))
        p.drawRoundedRect(panel, 6.0, 6.0)

        track = self.throbber_track()
        p.setPen(QColor(240, 236, 230))
        p.setFont(QFont("Fio UI", 11, QFont.Bold))
        fm = p.fontMetrics()
        p.drawText(QRectF(10, track.top() - fm.height() - 10, self.width() - 20, fm.height() + 4),
                   Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(self.title, Qt.ElideMiddle, self.width() - 24))

        # The track, and the line where the clock has carried it.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(70, 70, 76))
        p.drawRoundedRect(track, 1.0, 1.0)
        start, end = throbber_span(self.clock() - self._started, track.width())
        p.setBrush(FIO_ORANGE)
        p.drawRoundedRect(QRectF(track.left() + start, track.top() - 0.5,
                                 end - start, track.height() + 1.0), 1.5, 1.5)

        p.setPen(QColor(190, 188, 184))
        p.setFont(self.font())
        fm = p.fontMetrics()
        p.drawText(QRectF(10, track.bottom() + 9, self.width() - 20, fm.height() + 4),
                   Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(self.stage, Qt.ElideRight, self.width() - 24))
        p.end()


class _NullOverlay(QObject):
    """Stand-in where no window exists to cover (tools, tests)."""

    active = False

    def begin(self, *_a, **_k):
        pass

    def end(self):
        pass

    def step(self, *_a, **_k):
        pass

    @contextlib.contextmanager
    def busy(self, *_a, **_k):
        yield self


NULL_OVERLAY = _NullOverlay()


def overlay_for(window):
    """*window*'s loading overlay, created on first use (a null one if it has none)."""
    overlay = getattr(window, "_loading_overlay", None)
    if overlay is not None:
        return overlay
    if not isinstance(window, QWidget):
        return NULL_OVERLAY
    try:
        overlay = LoadingOverlay(window)
    except Exception:
        return NULL_OVERLAY
    window._loading_overlay = overlay
    return overlay
