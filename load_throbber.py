"""A loading throbber that keeps moving while the editor is busy.

Loading a very large level keeps the editor's GUI thread busy for seconds:
parsing the JSON, building the scene, refreshing every panel and painting the
first frames. None of that can yield to an animation in the same process --
most of it is Qt work that must run on the GUI thread, and the JSON parser
holds the GIL -- so the throbber runs as its own small process: a frameless,
always-on-top strip, five pixels tall, laid over the editor window, with
orange and green stripes scrolling across it.

The editor side is :class:`LoadThrobber`. The child is started through
``main.py --fio-load-throbber`` (the same executable in a packaged build) and
imports nothing but PyQt5, so it is on screen a fraction of a second after a
load starts. It closes when the editor closes its stdin -- or exits, or dies,
which closes the pipe too -- so it can never outlive the editor.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

#: Height of the throbber strip, in pixels.
HEIGHT = 5
#: Flag ``main.py`` dispatches on to run the throbber instead of the editor.
CHILD_FLAG = "--fio-load-throbber"

#: Levels smaller than this load quickly enough not to need a throbber.
MIN_LEVEL_BYTES = 4 * 1024 * 1024

ORANGE = (240, 128, 0)
GREEN = (46, 160, 67)
TRACK = (43, 43, 43)
STRIPE_WIDTH = 24           # px, measured along the bar
SCROLL_SPEED = 120.0        # px per second
FRAME_MS = 16
#: The child gives up on its own after this long, whatever happens.
MAX_LIFETIME_MS = 10 * 60 * 1000


# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------

def stripe_colour(x, offset, width=STRIPE_WIDTH):
    """Orange or green for the bar column at *x*, scrolled by *offset* px."""
    return ORANGE if int((x - offset) // width) % 2 == 0 else GREEN


def _make_widget(caption):
    from PyQt5.QtCore import QElapsedTimer, QRectF, Qt, QTimer
    from PyQt5.QtGui import QColor, QPainter, QPainterPath
    from PyQt5.QtWidgets import QWidget

    class ThrobberWidget(QWidget):
        def __init__(self):
            super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                             | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.setWindowTitle(caption)     # no room to draw it at 5 px
            self.offset = 0.0
            self._clock = QElapsedTimer()
            self._clock.start()
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(FRAME_MS)

        def _tick(self):
            self.offset = (self._clock.elapsed() / 1000.0 * SCROLL_SPEED) % (STRIPE_WIDTH * 2)
            self.update()

        def paintEvent(self, _event):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            bar = QRectF(0, 0, self.width(), self.height())
            p.fillRect(bar, QColor(*TRACK))
            # Slanted stripes, drawn as parallelograms scrolling to the right.
            slant = bar.height()
            x = int(bar.left() - slant - STRIPE_WIDTH * 2 + self.offset)
            p.setPen(Qt.NoPen)
            while x < bar.right() + slant:
                stripe = QPainterPath()
                stripe.moveTo(x, bar.bottom())
                stripe.lineTo(x + slant, bar.top())
                stripe.lineTo(x + slant + STRIPE_WIDTH, bar.top())
                stripe.lineTo(x + STRIPE_WIDTH, bar.bottom())
                stripe.closeSubpath()
                p.fillPath(stripe, QColor(*stripe_colour(x, self.offset)))
                x += STRIPE_WIDTH
            p.end()

    return ThrobberWidget()


def _watch_stdin(on_closed):
    """Call *on_closed* (from a thread) once stdin reaches end of file."""
    def run():
        try:
            while sys.stdin.buffer.read(1):
                pass
        except Exception:
            pass
        on_closed()
    threading.Thread(target=run, name="throbber-stdin", daemon=True).start()


def child_main(argv):
    """Run the throbber window: ``--fio-load-throbber X Y W H CAPTION``."""
    from PyQt5.QtCore import QObject, QTimer, pyqtSignal
    from PyQt5.QtWidgets import QApplication

    args = argv[argv.index(CHILD_FLAG) + 1:]
    try:
        x, y, w, h = (int(v) for v in args[:4])
    except ValueError:
        return 2
    caption = args[4] if len(args) > 4 else "Loading..."

    app = QApplication.instance() or QApplication([argv[0]])
    widget = _make_widget(caption)
    widget.setGeometry(x, y, max(120, w), max(1, h))
    widget.show()

    class _Closer(QObject):
        closed = pyqtSignal()

    closer = _Closer()
    closer.closed.connect(app.quit)              # queued to the GUI thread
    _watch_stdin(closer.closed.emit)
    QTimer.singleShot(MAX_LIFETIME_MS, app.quit)
    return app.exec_()


# ---------------------------------------------------------------------------
# Editor side
# ---------------------------------------------------------------------------

def child_command(caption, geometry, root_dir=None):
    """The command line that starts the throbber child.

    A packaged build is one executable that runs ``main.py``'s code, so the
    flag goes straight to it; from source, ``main.py`` is run by the same
    interpreter.
    """
    x, y, w, h = (int(v) for v in geometry)
    frozen = getattr(sys, "frozen", False) or "__compiled__" in globals()
    if frozen:
        cmd = [sys.executable]
    else:
        root = root_dir or os.path.dirname(os.path.abspath(__file__))
        cmd = [sys.executable, os.path.join(root, "main.py")]
    return cmd + [CHILD_FLAG, str(x), str(y), str(w), str(h), caption]


class LoadThrobber:
    """Shows the throbber process for the duration of a load.

    Every failure is swallowed: a throbber that cannot start must never stop
    a level from loading.
    """

    def __init__(self, caption, geometry, root_dir=None):
        self._command = child_command(caption, geometry, root_dir)
        self._proc = None

    def start(self):
        if self._proc is not None:
            return self
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                self._command, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
        except OSError as exc:
            print(f"[Loading] throbber unavailable: {exc}")
            self._proc = None
        return self

    def stop(self, timeout=1.0):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()                   # the child's signal to go
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception:
                pass

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None


def throbber_geometry(frame):
    """The throbber's rectangle over an editor window *frame* (x, y, w, h)."""
    fx, fy, fw, fh = frame
    w = max(240, min(520, int(fw * 0.4)))
    return (fx + (fw - w) // 2, fy + int(fh * 0.62), w, HEIGHT)
