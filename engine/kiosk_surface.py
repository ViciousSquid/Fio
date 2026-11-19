"""Native presentation for kiosk mode: the same renderer, on a window of its own.

In the editor (and in kiosk mode by default) frames are presented by the 3D
view, a ``QOpenGLWidget``: Fio renders into the widget's framebuffer object
and Qt then composites that image into the window on every frame. With
``[Kiosk] native_surface = True`` kiosk mode presents on a native window
instead -- this module's :class:`NativeKioskSurface` -- with no compositing
step.

What it is not: a second renderer, a second GL context or a second runtime.
The surface owns no GL objects. Each frame the view's own ``QOpenGLContext``
is made current on the surface and the view's own ``paintGL`` draws the
published frame -- the same renderer, render passes, GL resources and dense
tables as the widget -- into the surface's default framebuffer, which is then
swapped. GL container objects (VAOs, FBOs) are per context and would not
survive a second, shared context; one context on two surfaces keeps them all.

The view consults :attr:`QtGameView._native_surface` for its geometry,
focus, mouse capture and repaint requests while the surface presents
(QtGameView, "presentation target"); with no surface it is the widget, as
always. Input the surface receives is delivered to the view with
``QApplication.sendEvent`` so it propagates (to the editor window's key
handling) and passes the same event filters as input that reaches the view
directly.
"""

from __future__ import annotations

import time

import OpenGL.GL as gl
from PyQt5.QtCore import QEvent, QSize, Qt, QTimer
from PyQt5 import sip
from PyQt5.QtGui import (QOpenGLContext, QOpenGLPaintDevice, QSurface,
                         QSurfaceFormat, QWindow)
from PyQt5.QtWidgets import QApplication


def _same_object(a, b):
    """Whether two PyQt wrappers stand for the same C++ object."""
    if a is None or b is None:
        return False
    return sip.unwrapinstance(a) == sip.unwrapinstance(b)


class NativePresentationError(RuntimeError):
    """The view's GL context cannot present on a native window here."""


def format_mismatch(context_format: QSurfaceFormat, surface_format: QSurfaceFormat):
    """Why a context with *context_format* cannot be made current on a surface
    with *surface_format* -- a short reason -- or None when they agree.

    The context's pixel format decides what it can draw to (on Windows a
    window's pixel format is fixed once set; GLX and EGL configs must match
    too), so the parts that choose it must agree: the renderable type and
    the colour, depth, stencil and multisample buffers. Version and profile
    are the context's, not the surface's (an EGL window surface reports no
    profile at all), so they are not compared. The final word is
    makeCurrent, which the caller still checks.
    """
    checks = (
        ('renderable type', context_format.renderableType(), surface_format.renderableType()),
        ('red bits', context_format.redBufferSize(), surface_format.redBufferSize()),
        ('green bits', context_format.greenBufferSize(), surface_format.greenBufferSize()),
        ('blue bits', context_format.blueBufferSize(), surface_format.blueBufferSize()),
        ('depth bits', context_format.depthBufferSize(), surface_format.depthBufferSize()),
        ('stencil bits', context_format.stencilBufferSize(), surface_format.stencilBufferSize()),
        ('samples', max(0, context_format.samples()), max(0, surface_format.samples())),
    )
    for name, wanted, got in checks:
        # -1 is "don't care" in a requested format.
        if wanted != got and wanted != -1 and got != -1:
            return '%s: context %s, surface %s' % (name, wanted, got)
    return None


#: Input the surface hands to the view. Everything else (expose, resize,
#: close, focus) the surface handles itself.
_FORWARDED_EVENTS = frozenset({
    QEvent.KeyPress, QEvent.KeyRelease, QEvent.ShortcutOverride,
    QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick,
    QEvent.MouseMove, QEvent.Wheel,
})


class NativeKioskSurface(QWindow):
    """A native window the 3D view's GL context presents kiosk frames on."""

    def __init__(self, view):
        super().__init__()
        # Set before anything can deliver an event: Windows sends expose and
        # resize while the native window is being created, before the
        # constructor returns.
        self.view = view
        self._device = None
        #: Seconds the last swap took (PresentationStats).
        self.last_swap_ms = 0.0
        #: For tests: a callable given the framebuffer just drawn, before the
        #: swap (it is undefined after it).
        self.before_swap = None
        # One frame request at a time, coalesced. A timer of the surface's
        # own rather than QTimer.singleShot(0, bound method): it goes with
        # the surface, so no request queued at the end of a turn outlives it.
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(0)
        self._update_timer.timeout.connect(self._deliver_update)
        context = view.context()
        if context is None or not context.isValid():
            raise NativePresentationError("the 3D view has no valid OpenGL context")
        self.context = context
        self.setSurfaceType(QSurface.OpenGLSurface)
        # The same pixel format as the context, so the context can draw here.
        self.setFormat(context.format())
        self.setTitle(view.window().windowTitle() if view.window() else "Fio")
        icon = view.window().windowIcon() if view.window() else None
        if icon is not None and not icon.isNull():
            self.setIcon(icon)
        self.create()
        mismatch = format_mismatch(context.format(), self.format())
        if mismatch:
            self.destroy()
            raise NativePresentationError("incompatible surface (%s)" % mismatch)

    def presenting(self):
        """Whether the view presents here now. Not yet while the window is
        being created (Windows delivers its first expose and resize then),
        and no longer once presentation has ended and the window waits to be
        deleted: events then must not touch the view."""
        return getattr(self.view, '_native_surface', None) is self

    # -- presenting --------------------------------------------------------

    def pixel_size(self):
        ratio = self.devicePixelRatio()
        return (max(1, int(round(self.width() * ratio))),
                max(1, int(round(self.height() * ratio))))

    def paint_device(self):
        """The QOpenGLPaintDevice the frame's QPainter (HUD, overlays) uses:
        the surface's default framebuffer at its pixel size.

        A QOpenGLPaintDevice belongs to the context current when it was
        made; one made with none current paints nothing, silently -- the HUD
        vanished from native frames when the device was first asked for
        outside a frame. So it is made, or made again, only while this
        surface's context is current, and never handed out otherwise."""
        if not self._context_current():
            raise NativePresentationError(
                "the paint device is asked for while the context is not current")
        size = QSize(*self.pixel_size())
        device = self._device
        if (device is None or device.size() != size
                or not _same_object(device.context(), self.context)):
            device = self._device = QOpenGLPaintDevice(size)
        device.setDevicePixelRatio(self.devicePixelRatio())
        return device

    def _context_current(self):
        return _same_object(QOpenGLContext.currentContext(), self.context)

    def make_current(self):
        """Make the view's context current on this surface, or raise."""
        if not self.isExposed():
            return False
        if not self.context.makeCurrent(self):
            raise NativePresentationError("makeCurrent on the native surface failed")
        return True

    def present(self):
        """Draw the newest published frame here and swap. Returns whether a
        frame was drawn (False while the window is not exposed)."""
        self._update_timer.stop()
        if not self.make_current():
            return False
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.context.defaultFramebufferObject())
        width, height = self.pixel_size()
        gl.glViewport(0, 0, width, height)
        self.view._paint_presented_frame()
        if self.before_swap is not None:
            self.before_swap(width, height)
        started = time.perf_counter()
        self.context.swapBuffers(self)
        self.last_swap_ms = (time.perf_counter() - started) * 1000.0
        return True

    def schedule_update(self):
        """Ask for a frame (the view's update(), while this presents): drawn
        on the event loop's next turn, as a widget's update() is. Not
        QWindow.requestUpdate(): Qt delivers that on its own refresh-rate
        timer, which put a published frame back on a ~16 ms grid -- the
        doubled frames painting on publication had removed."""
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _deliver_update(self):
        if self.presenting():
            self.view._present_native_frame()

    # -- events ------------------------------------------------------------

    def event(self, event):
        kind = event.type()
        if not self.presenting():
            # Being created, or ended: the view is the widget's again.
            if kind == QEvent.Close:
                event.ignore()
                return True
            return super().event(event)
        if kind == QEvent.UpdateRequest:
            self.view._present_native_frame()
            return True
        if kind in _FORWARDED_EVENTS:
            # Delivered as if it had reached the view: it propagates to the
            # editor window's key handling and passes the same filters.
            QApplication.sendEvent(self.view, event)
            return True
        if kind == QEvent.InputMethodQuery:
            # The editor's held-key filter answers this for its widgets; the
            # surface is not one. A held W must not open macOS's accent
            # picker over the game.
            if event.queries() & Qt.ImEnabled:
                event.setValue(Qt.ImEnabled, False)
                event.accept()
                return True
        if kind == QEvent.Close:
            # Closing the game window asks, as Escape's Quit does; a No keeps
            # it open.
            event.ignore()
            self.view.editor.leave_play_mode_from_escape()
            return True
        if kind in (QEvent.FocusIn, QEvent.FocusOut):
            self.view._native_focus_changed(kind == QEvent.FocusIn)
        return super().event(event)

    def exposeEvent(self, event):
        if self.presenting() and self.isExposed():
            self.schedule_update()

    def resizeEvent(self, event):
        if self.presenting():
            self.view._apply_viewport_size(self.width(), self.height())
            self.schedule_update()

    # -- window modes ---------------------------------------------------------

    def show_in_mode(self, mode, width, height, screen=None):
        """Show as kiosk *mode* (``MainWindow.KIOSK_WINDOW_MODES``):
        Fullscreen, Borderless (a frameless window over the whole screen) or
        Windowed (*width* x *height*, centred) -- what the editor window
        does in kiosk mode without a native surface."""
        if screen is not None and self.screen() is not screen:
            self.setScreen(screen)
        screen = self.screen()
        self.setFlag(Qt.FramelessWindowHint, mode == 'Borderless')
        if mode == 'Fullscreen':
            self.showFullScreen()
            return
        self.showNormal()
        if mode == 'Borderless':
            self.setGeometry(screen.geometry())
            return
        area = screen.availableGeometry()
        width, height = min(width, area.width()), min(height, area.height())
        self.resize(width, height)
        self.setPosition(area.x() + (area.width() - width) // 2,
                         area.y() + (area.height() - height) // 2)


class PresentationStats:
    """Frame timings of whichever path presents -- the widget (Qt composes
    its framebuffer into the window) or the native kiosk window -- recorded
    the same way for both, so the two can be compared under the same
    conditions (tools/bench_kiosk_presentation.py, the ``r_presentation``
    console command).

    Per presented frame:

    * the interval since the previous presented frame;
    * render time: paintGL's CPU time (the GL work it queues may finish
      later, inside the swap);
    * presentation time -- widget: from the end of paintGL to Qt's
      ``frameSwapped`` (Qt composites the widget's framebuffer, then swaps);
      native: ``swapBuffers`` alone;
    * which publication it showed: a frame showing the same one as the frame
      before is a duplicate; publications between two shown frames were
      skipped (superseded before a paint took them);
    * latency: from the logic thread preparing the frame to its presentation
      returning.

    A short swap is not a frame on the display sooner: a swap may return
    before the frame is scanned out (the driver queues it) and the wait then
    lands on a later call. Read presentation time together with the
    interval and latency, which do include it.
    """

    def __init__(self, keep=1200):
        from collections import deque
        self.frames = {'widget': deque(maxlen=keep), 'native': deque(maxlen=keep)}

    def record(self, path, interval_ms, paint_ms, present_ms, serial=None, latency_ms=None):
        self.frames[path].append((interval_ms, float(paint_ms or 0.0), float(present_ms or 0.0),
                                  serial, latency_ms))

    def clear(self):
        for frames in self.frames.values():
            frames.clear()

    @staticmethod
    def _pct(values, q):
        values = sorted(values)
        return values[min(len(values) - 1, int(len(values) * q))] if values else None

    def summary(self, path):
        """Medians and tails of one path's recorded frames (None if none)."""
        frames = list(self.frames[path])
        if not frames:
            return None
        intervals = [f[0] for f in frames if f[0] is not None]
        median = self._pct(intervals, 0.5)
        serials = [f[3] for f in frames if f[3]]
        duplicates = sum(1 for a, b in zip(serials, serials[1:]) if b == a)
        skipped = sum(b - a - 1 for a, b in zip(serials, serials[1:]) if b > a + 1)
        latencies = [f[4] for f in frames if f[4] is not None]
        return {
            'frames': len(frames),
            'fps': (1000.0 / median) if median else None,
            'interval_ms_p50': median,
            'interval_ms_p95': self._pct(intervals, 0.95),
            'interval_ms_p99': self._pct(intervals, 0.99),
            'doubled_frames': (sum(1 for i in intervals if i > 1.5 * median)
                               if median else 0),
            'paint_ms_p50': self._pct([f[1] for f in frames], 0.5),
            'present_ms_p50': self._pct([f[2] for f in frames], 0.5),
            'present_ms_p95': self._pct([f[2] for f in frames], 0.95),
            'duplicate_frames': duplicates,
            'skipped_publications': skipped,
            'latency_ms_p50': self._pct(latencies, 0.5),
            'latency_ms_p95': self._pct(latencies, 0.95),
        }

    def report(self):
        """Both paths, one line each, for the console."""
        lines = []
        for path in ('widget', 'native'):
            s = self.summary(path)
            if s is None:
                lines.append('%-6s  no frames recorded' % path)
                continue
            lines.append(
                '%-6s  %4d frames  %5.1f fps  interval p50 %.2f p99 %.2f ms  doubled %d  '
                'render %.2f ms  present p50 %.2f p95 %.2f ms  latency p50 %.2f ms  '
                'duplicates %d  skipped %d' % (
                    path, s['frames'], s['fps'] or 0.0, s['interval_ms_p50'] or 0.0,
                    s['interval_ms_p99'] or 0.0, s['doubled_frames'],
                    s['paint_ms_p50'], s['present_ms_p50'], s['present_ms_p95'],
                    s['latency_ms_p50'] or 0.0, s['duplicate_frames'],
                    s['skipped_publications']))
        return '\n'.join(lines)
