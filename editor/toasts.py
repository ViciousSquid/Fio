"""Slide-in toasts: the editor's notifications for what matters.

Only important things are toasted -- loads and saves (``success``), warnings
and errors; tips, hints and routine feedback are not (see
:meth:`editor.main_window.MainWindow.show_toast`). A toast is a card: a dark tinted body, an accent bar down its left
side with a short foot along the bottom, an icon, the message in the accent's
colour, and copy and close buttons. Toasts sit in the
window's bottom-right corner, just above the bottom bar (the host says where:
:class:`ToastManager`'s *anchor*); a new one slides in from the right edge at
the bottom of the stack, pushing older ones up, and slides out when it
expires or is closed. Hovering a toast holds it; clicking it pins it (it
stays until its close button is pressed, and shows a pin; clicking the pin
unpins it, and it shows an unpinned pin). Its text can be selected and
copied (its Copy button takes it all; or drag, Ctrl/Cmd+C, or
right-click > Copy / Select All).

Qt-only, no editor imports: :class:`ToastManager` takes any window widget.
"""

from PyQt5.QtCore import (QEasingCurve, QEvent, QObject, QPoint,
                          QPropertyAnimation, QRectF, Qt, QTimer)
from PyQt5.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
                         QPixmap, QTransform)
from PyQt5.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QSizePolicy, QToolButton, QVBoxLayout,
                             QWidget)

#: level -> (accent, body, text). Success is Fio's green, warning its orange.
LEVELS = {
    'success': ('#4CAF50', '#14231a', '#8fdc98'),
    'info':    ('#3d8ee8', '#121f2d', '#82bdf6'),
    'warning': ('#F08000', '#2a1e0f', '#f7b866'),
    'error':   ('#e5533d', '#2b1613', '#f3836f'),
}
#: How long each level stays up, in ms: three seconds, but an error lingers.
DURATIONS = {'success': 3000, 'info': 3000, 'warning': 3000, 'error': 8000}

WIDTH = 320
MARGIN = 12          # from the window's edge and the anchor
GAP = 5              # between stacked toasts
MAX_TOASTS = 4
SLIDE_MS = 260


def pin_icon(colour, pinned, size=16):
    """A pushpin drawn in *colour*: upright and filled when *pinned*; tilted
    and outlined when not (the usual "unpinned" pin)."""
    scale = 4                                    # drawn large, then smoothed
    side = size * scale
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    if not pinned:
        painter.setTransform(QTransform().translate(side / 2, side / 2)
                             .rotate(45).translate(-side / 2, -side / 2))
    u = side / 16.0
    pen = QPen(QColor(colour), 1.4 * u)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QColor(colour) if pinned else Qt.NoBrush)
    head = QPainterPath()                        # the pin's head and collar
    head.moveTo(5 * u, 1.5 * u)
    head.lineTo(11 * u, 1.5 * u)
    head.lineTo(10 * u, 3.5 * u)
    head.lineTo(10 * u, 7 * u)
    head.lineTo(12.5 * u, 9.5 * u)
    head.lineTo(3.5 * u, 9.5 * u)
    head.lineTo(6 * u, 7 * u)
    head.lineTo(6 * u, 3.5 * u)
    head.closeSubpath()
    painter.drawPath(head)
    painter.drawLine(QPoint(int(8 * u), int(9.5 * u)), QPoint(int(8 * u), int(15 * u)))
    painter.end()
    return QIcon(pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class Toast(QWidget):
    """One notification card. Closed with :meth:`dismiss` (or its ✕)."""

    def __init__(self, manager, message, level):
        super().__init__(manager.window)
        self.manager = manager
        self.level = level if level in LEVELS else 'info'
        self.message = str(message)
        self.accent, self.body, self.ink = (QColor(c) for c in LEVELS[self.level])
        self.closing = False
        #: Clicked: stays until closed with its button.
        self.pinned = False
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFixedWidth(WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(36, 5, 4, 5)      # room for the bar and icon
        outer.setSpacing(4)
        column = QVBoxLayout()
        column.setSpacing(4)
        self.label = QLabel(self.message)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF(), 9.5))
        self.label.setFont(font)
        self.label.setStyleSheet(f"color: {self.ink.name()}; background: transparent;")
        # Selectable, so it can be copied (and right-click offers Copy).
        self.label.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.label.setCursor(Qt.IBeamCursor)
        self.label.installEventFilter(self)
        column.addWidget(self.label)
        outer.addLayout(column, 1)
        self.close_button = QToolButton()
        self.close_button.setText("✕")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setToolTip("Dismiss")
        self.close_button.setStyleSheet(
            f"QToolButton {{ color: {self.ink.name()}; background: transparent; border: none;"
            " font-size: 11px; padding: 0px 3px; }"
            "QToolButton:hover { color: #ffffff; }")
        self.close_button.clicked.connect(self.dismiss)
        # Copy: the whole message to the clipboard (the text can also be
        # selected and copied a part at a time).
        self.copy_button = QToolButton()
        self.copy_button.setText("\u29c9")
        self.copy_button.setCursor(Qt.PointingHandCursor)
        self.copy_button.setToolTip("Copy the message")
        self.copy_button.setStyleSheet(self.close_button.styleSheet())
        self.copy_button.clicked.connect(self.copy)
        # The pin: shown once the toast is pinned (by a click on it). Pinned,
        # it says why the toast has not gone; clicking it unpins (the toast
        # goes on its own again) and it shows an unpinned pin, which pins
        # it again.
        self.pin_button = QToolButton()
        self.pin_button.setCursor(Qt.PointingHandCursor)
        self.pin_button.setStyleSheet(
            "QToolButton { background: transparent; border: none; padding: 0px 3px; }"
            "QToolButton:hover { background: rgba(255,255,255,0.10); border-radius: 4px; }")
        self.pin_button.clicked.connect(self.toggle_pin)
        self.pin_button.hide()
        buttons = QHBoxLayout()
        buttons.setSpacing(0)
        buttons.addWidget(self.pin_button)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)
        outer.setAlignment(buttons, Qt.AlignTop)

        self.opacity = QGraphicsOpacityEffect(self)
        self.opacity.setOpacity(1.0)
        self.setGraphicsEffect(self.opacity)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)
        self._animations = []
        self.fit_height()

    def fit_height(self):
        """As tall as its (wrapped) text needs at its width -- adjustSize()
        sized wrapped text for a narrower width and left cards too tall."""
        layout = self.layout()
        if layout.hasHeightForWidth():
            height = layout.totalHeightForWidth(WIDTH)
        else:
            height = layout.totalSizeHint().height()
        self.setFixedHeight(max(height, 30))

    # -- lifetime -------------------------------------------------------------

    def start(self, duration):
        """Keep the toast up *duration* ms (0 or less: until closed)."""
        self._duration = int(duration or 0)
        if self._duration > 0 and not self.pinned:
            self.timer.start(self._duration)

    def enterEvent(self, event):
        self.timer.stop()                       # held while the cursor is on it
        super().enterEvent(event)

    def leaveEvent(self, event):
        if (getattr(self, '_duration', 0) > 0 and not self.closing
                and not self.pinned):
            self.timer.start(max(1500, self._duration // 2))
        super().leaveEvent(event)

    def pin(self):
        """Keep the toast until its close button is pressed (a click on it)."""
        if self.pinned or self.closing:
            return
        self.pinned = True
        self.timer.stop()
        self._show_pin()

    def unpin(self):
        """Let the toast go on its own again (its pin clicked)."""
        if not self.pinned or self.closing:
            return
        self.pinned = False
        self.timer.start(max(2500, getattr(self, '_duration', 0) or 0))
        self._show_pin()

    def toggle_pin(self):
        if self.pinned:
            self.unpin()
        else:
            self.pin()

    def _show_pin(self):
        self.pin_button.setIcon(pin_icon(self.accent, self.pinned, size=13))
        self.pin_button.setToolTip(
            "Pinned: stays until closed. Click to unpin." if self.pinned
            else "Unpinned: will go on its own. Click to pin.")
        self.pin_button.show()
        self.update()

    def mousePressEvent(self, event):
        self.pin()
        super().mousePressEvent(event)

    def eventFilter(self, obj, event):
        # A press on the (selectable) text pins the toast too.
        if obj is self.label and event.type() == QEvent.MouseButtonPress:
            self.pin()
        return False

    def text(self):
        """The message, as it would be copied."""
        return self.label.text()

    def copy(self):
        """Put the whole message on the clipboard (the Copy button). Copying
        pins the toast: it was wanted."""
        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(self.text())
        self.pin()
        self.copy_button.setText("\u2713")
        # The toast's own timer: it goes with the toast, so it never fires
        # on a deleted button.
        timer = getattr(self, '_copied_timer', None)
        if timer is None:
            timer = self._copied_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self.copy_button.setText("\u29c9"))
        timer.start(1200)

    def dismiss(self):
        """Slide out to the right and go."""
        if self.closing:
            return
        self.closing = True
        self.timer.stop()
        self.manager._closing(self)
        end = QPoint(self.manager.window.width() + 8, self.y())
        self._animate(end, 1.0, 0.0, QEasingCurve.InCubic, self._gone)

    def _gone(self):
        self.manager._remove(self)
        self.hide()
        self.deleteLater()

    def _animate(self, end, start_opacity, end_opacity, curve, finished=None):
        slide = QPropertyAnimation(self, b"pos", self)
        slide.setDuration(SLIDE_MS)
        slide.setEndValue(end)
        slide.setEasingCurve(curve)
        fade = QPropertyAnimation(self.opacity, b"opacity", self)
        fade.setDuration(SLIDE_MS)
        fade.setStartValue(start_opacity)
        fade.setEndValue(end_opacity)
        if finished is not None:
            slide.finished.connect(finished)
        self._animations = [slide, fade]
        slide.start()
        fade.start()

    # -- drawing --------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 6.0
        card = QPainterPath()
        card.addRoundedRect(rect, radius, radius)
        painter.fillPath(card, self.body)
        painter.setClipPath(card)
        # The accent bar down the left, and its short foot along the bottom.
        painter.fillRect(QRectF(rect.left(), rect.top(), 3.0, rect.height()), self.accent)
        painter.fillRect(QRectF(rect.left(), rect.bottom() - 2.0, 20.0, 2.0), self.accent)
        painter.setClipping(False)
        if self.pinned:
            # Pinned: outlined in its accent.
            painter.setPen(QPen(self.accent, 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        self._paint_icon(painter, QRectF(11, (self.height() - 16) / 2.0, 16, 16))

    def _paint_icon(self, painter, box):
        pen = QPen(self.accent, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        c = box.center()
        r = box.width() / 2.0 - 1.0
        if self.level == 'warning':
            path = QPainterPath()
            path.moveTo(c.x(), box.top() + 1)
            path.lineTo(box.right() - 1, box.bottom() - 2)
            path.lineTo(box.left() + 1, box.bottom() - 2)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(QPoint(int(c.x()), int(c.y() - r * 0.15)),
                             QPoint(int(c.x()), int(c.y() + r * 0.3)))
            painter.drawPoint(QPoint(int(c.x()), int(c.y() + r * 0.6)))
            return
        painter.drawEllipse(c, r, r)
        if self.level == 'error':
            d = r * 0.42
            painter.drawLine(QPoint(int(c.x() - d), int(c.y() - d)), QPoint(int(c.x() + d), int(c.y() + d)))
            painter.drawLine(QPoint(int(c.x() + d), int(c.y() - d)), QPoint(int(c.x() - d), int(c.y() + d)))
        elif self.level == 'success':
            path = QPainterPath()
            path.moveTo(c.x() - r * 0.45, c.y() + r * 0.02)
            path.lineTo(c.x() - r * 0.1, c.y() + r * 0.38)
            path.lineTo(c.x() + r * 0.5, c.y() - r * 0.35)
            painter.drawPath(path)
        else:
            painter.drawLine(QPoint(int(c.x()), int(c.y() - 1)), QPoint(int(c.x()), int(c.y() + r * 0.5)))
            painter.drawPoint(QPoint(int(c.x()), int(c.y() - r * 0.45)))


class ToastManager(QObject):
    """The stack of toasts in *window*'s bottom-right corner.

    *anchor*, when given, returns the y (in window coordinates) the stack
    stands on -- the top of the editor's bottom bar; by default the window's
    bottom edge.
    """

    def __init__(self, window, anchor=None):
        super().__init__(window)
        self.window = window
        self.anchor = anchor
        self.toasts = []
        window.installEventFilter(self)

    def show(self, message, level='info', duration=None):
        """Slide in a toast; return it. The same message again while it is
        still up restarts that toast instead of stacking a copy."""
        message = str(message)
        for toast in self.toasts:
            if not toast.closing and toast.message == message and toast.level == level:
                toast.start(DURATIONS.get(level, 3000) if duration is None else duration)
                return toast
        live = [t for t in self.toasts if not t.closing]
        for old in live[:max(0, len(live) - MAX_TOASTS + 1)]:
            old.dismiss()
        toast = Toast(self, message, level)
        self.toasts.append(toast)
        toast.move(self.window.width() + 8, self._bottom() - toast.height())
        toast.show()
        toast.raise_()
        self._restack(new=toast)
        toast.start(DURATIONS.get(level, 3000) if duration is None else duration)
        return toast

    def messages(self):
        """``[(level, message)]`` of the toasts showing, oldest first."""
        return [(t.level, t.message) for t in self.toasts if not t.closing]

    def clear(self):
        for toast in list(self.toasts):
            toast.dismiss()

    # -- layout -----------------------------------------------------------------

    def _bottom(self):
        """The y the stack's lowest toast ends at."""
        base = self.anchor() if self.anchor is not None else self.window.height()
        return int(base) - MARGIN

    def _restack(self, new=None):
        """Newest at the bottom, just above the anchor; older ones above it."""
        x = max(MARGIN, self.window.width() - WIDTH - MARGIN)
        y = self._bottom()
        for toast in reversed(self.toasts):
            if toast.closing:
                continue
            toast.fit_height()
            y -= toast.height()
            target = QPoint(x, y)
            if toast is new:
                toast._animate(target, 0.0, 1.0, QEasingCurve.OutCubic)
            elif toast.pos() != target:
                slide = QPropertyAnimation(toast, b"pos", toast)
                slide.setDuration(SLIDE_MS)
                slide.setEndValue(target)
                slide.setEasingCurve(QEasingCurve.OutCubic)
                toast._animations = [slide]
                slide.start()
            toast.raise_()
            y -= GAP

    def _closing(self, toast):
        QTimer.singleShot(0, self._restack)

    def _remove(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Resize:
            self._restack()
        return False
