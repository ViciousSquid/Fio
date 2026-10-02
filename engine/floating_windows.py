"""
A tiny floating-window manager for QtGameView overlays.

SysMon (``engine.sysmon``) is a single draggable/collapsible overlay painted on
top of the 3D view. This module generalises that idea so the view can host
*several* such popups -- each clickable, draggable and collapsible exactly like
SysMon -- managed together with correct z-ordering and mouse routing.

Pieces:
  * :class:`FloatingWindow` -- the reusable chrome (title bar, drag, collapse,
    close). Subclass it and override :meth:`content_height` / :meth:`draw_body`.
  * :class:`WindowManager` -- owns a stack of windows, paints them back-to-front,
    and routes mouse events front-to-back (topmost wins, and is raised).
  * :class:`CallbackWindow` -- a window whose body is painted by a supplied
    callback, so a caller can present its own overlay through the same manager
    without subclassing anything.

All Qt drawing mirrors SysMon's look so the popups feel like one family. The
manager is deliberately engine-light: it only needs a QPainter to draw and Qt
mouse events to route, so QtGameView can drop it in with a few lines. It holds
no engine, editor or game imports.
"""

from PyQt5.QtCore import Qt, QRect, QPoint
from PyQt5.QtGui import QPainter, QColor, QFont, QPen, QBrush, QFontMetrics


# Shared palette (matches engine.sysmon so popups look like one family).
_BG = QColor(20, 20, 25, 235)
_BORDER = QColor(80, 80, 90)
_HEADER = QColor(66, 95, 93)
_HEADER_ACTIVE = QColor(90, 130, 128)
_TEXT = QColor(220, 220, 220)
_WHITE = QColor(255, 255, 255)
_MUTED = QColor(150, 150, 160)
_ACCENT = QColor(240, 128, 0)
_GOOD = QColor(100, 220, 120)
_BAD = QColor(230, 90, 90)
_BAR_BG = QColor(45, 45, 55)


class FloatingWindow:
    """Draggable, collapsible, closable overlay panel -- SysMon-style chrome."""

    HEADER_H = 25
    MIN_W = 180

    def __init__(self, title, x=40, y=40, width=340, body_height=200):
        self.title = title
        self.width = max(self.MIN_W, int(width))
        self._body_height = int(body_height)
        self.window_rect = QRect(int(x), int(y), self.width, self.HEADER_H)
        self.expanded = True
        self.active = True
        self.dragging = False
        self.drag_offset = QPoint(0, 0)

        self.font = QFont("Arial", 9)
        self.font.setStyleHint(QFont.Monospace)
        self.title_font = QFont("Arial", 9)
        self.title_font.setBold(True)
        self._fm = QFontMetrics(self.font)

    # -- overridable content ------------------------------------------
    def content_height(self):
        """Pixel height of the body when expanded. Override for dynamic size."""
        return self._body_height

    def draw_body(self, painter, x, y, w):
        """Paint the body. Override in subclasses. (x, y) is the body's top-left."""
        painter.setPen(_TEXT)
        painter.setFont(self.font)
        painter.drawText(x + 10, y + 20, "(empty)")

    def on_close(self):
        """Hook called when the window is closed via its [X]. Override if needed."""
        pass

    # -- geometry helpers ---------------------------------------------
    def _full_rect(self):
        h = self.HEADER_H + (self.content_height() if self.expanded else 0)
        return QRect(self.window_rect.x(), self.window_rect.y(), self.width, h)

    # -- drawing ------------------------------------------------------
    def draw(self, painter, focused=False):
        rect = self._full_rect()
        # Keep window_rect height in sync so hit-testing matches what's drawn.
        self.window_rect.setHeight(rect.height())

        painter.setPen(QPen(_BORDER, 1))
        painter.setBrush(QBrush(_BG))
        painter.drawRect(rect)

        header = QRect(rect.x(), rect.y(), rect.width(), self.HEADER_H)
        painter.fillRect(header, _HEADER_ACTIVE if focused else _HEADER)
        painter.setPen(QPen(_BORDER, 1))
        painter.drawLine(rect.x(), rect.y() + self.HEADER_H,
                         rect.right(), rect.y() + self.HEADER_H)

        painter.setFont(self.title_font)
        painter.setPen(_WHITE)
        title = self._fm.elidedText(self.title, Qt.ElideRight, rect.width() - 70)
        painter.drawText(header.adjusted(10, 0, 0, 0),
                         Qt.AlignVCenter | Qt.AlignLeft, title)
        painter.drawText(QRect(rect.right() - 50, rect.y(), 25, self.HEADER_H),
                         Qt.AlignCenter, "\u25bc" if self.expanded else "\u25b6")
        painter.drawText(QRect(rect.right() - 25, rect.y(), 25, self.HEADER_H),
                         Qt.AlignCenter, "[X]")

        if self.expanded:
            self.draw_body(painter, rect.x(), rect.y() + self.HEADER_H, rect.width())

    # -- mouse --------------------------------------------------------
    def hit(self, pos):
        return self._full_rect().contains(pos)

    def handle_mouse_press(self, event):
        """Return True if this window consumed the press."""
        if not self.active:
            return False
        rect = self._full_rect()
        if not rect.contains(event.pos()):
            return False
        # Close button
        if event.x() > rect.right() - 25 and event.y() < rect.y() + self.HEADER_H:
            self.active = False
            try:
                self.on_close()
            except Exception as exc:
                print(f"[FloatingWindow] on_close failed for '{self.title}': {exc}")
            return True
        # Title bar: collapse toggle or drag
        title_bar = QRect(rect.x(), rect.y(), rect.width(), self.HEADER_H)
        if title_bar.contains(event.pos()):
            if event.x() > rect.right() - 50:
                self.expanded = not self.expanded
                return True
            self.dragging = True
            self.drag_offset = event.pos() - QPoint(rect.x(), rect.y())
            return True
        # Body click: let a subclass act on it (tabs, list rows...), then swallow
        # it so it never leaks to the view beneath.
        if self.expanded:
            try:
                self.handle_body_click(event.x(), event.y())
            except Exception as exc:
                print(f"[FloatingWindow] body click failed for '{self.title}': {exc}")
        return True

    def handle_body_click(self, x, y):
        """Hook: handle a click inside the body. Override in subclasses. (x, y)
        are view-space pixels. Default: do nothing (the click is still swallowed)."""
        return False

    def handle_mouse_move(self, event, view_w, view_h):
        if not self.dragging:
            return False
        new_pos = event.pos() - self.drag_offset
        new_x = max(5, min(new_pos.x(), max(5, view_w - self.width - 5)))
        new_y = max(5, min(new_pos.y(), max(5, view_h - self.HEADER_H - 5)))
        self.window_rect.moveTo(new_x, new_y)
        return True

    def handle_mouse_release(self, event):
        if self.dragging:
            self.dragging = False
            return True
        return False


class WindowManager:
    """Owns a z-ordered stack of :class:`FloatingWindow`s and routes to them."""

    def __init__(self):
        self.windows = []          # bottom-to-top; last == topmost/focused

    # -- lifecycle ----------------------------------------------------
    def add(self, window):
        if window not in self.windows:
            self.windows.append(window)
        else:
            self.raise_(window)
        return window

    def remove(self, window):
        if window in self.windows:
            self.windows.remove(window)

    def raise_(self, window):
        if window in self.windows and self.windows[-1] is not window:
            self.windows.remove(window)
            self.windows.append(window)

    def prune(self):
        """Drop closed windows. Call once per frame."""
        self.windows = [w for w in self.windows if getattr(w, "active", False)]

    def clear(self):
        self.windows = []

    def find(self, predicate):
        for w in self.windows:
            try:
                if predicate(w):
                    return w
            except Exception as exc:
                print(f"[WindowManager] find predicate failed: {exc}")
                continue
        return None

    # -- drawing ------------------------------------------------------
    def draw_all(self, painter):
        self.prune()
        top = self.windows[-1] if self.windows else None
        for w in self.windows:
            if getattr(w, "active", False):
                try:
                    w.draw(painter, focused=(w is top))
                except Exception as exc:
                    # One misbehaving overlay must not abort the whole frame's
                    # paint, but the failure is still reported.
                    print(f"[WindowManager] draw failed for "
                          f"'{getattr(w, 'title', '?')}': {exc}")

    # -- mouse routing (topmost first) --------------------------------
    def handle_mouse_press(self, event):
        for w in reversed(self.windows):
            if w.active and w.hit(event.pos()):
                consumed = w.handle_mouse_press(event)
                self.raise_(w)
                self.prune()
                return consumed
        return False

    def handle_mouse_move(self, event, view_w, view_h):
        for w in reversed(self.windows):
            if w.dragging:
                return w.handle_mouse_move(event, view_w, view_h)
        return False

    def handle_mouse_release(self, event):
        handled = False
        for w in self.windows:
            if w.handle_mouse_release(event):
                handled = True
        return handled


class CallbackWindow(FloatingWindow):
    """A floating window whose body is painted by a supplied callback.

    Lets a caller present its own overlay popups as draggable / collapsible /
    closable windows through the same :class:`WindowManager`, without
    subclassing. The popup keeps drawing with the live QPainter each frame, so
    it stays in sync with whatever state it reads, while the manager provides
    the chrome and mouse routing.

    ``draw_fn(painter, x, y, w, h)`` paints the body (top-left ``x, y``).
    ``on_close_cb`` (optional) is called when the window's [X] is clicked.
    ``on_body_click_cb(x, y)`` (optional) is called for a click inside the body
    (view-space pixels); return True to consume it. ``wants_cursor`` tells the
    play-mode view to free the (normally hidden, centre-locked) cursor while
    this window is open so its body can be clicked.
    """

    def __init__(self, key, title, draw_fn, width=520, body_height=360,
                 x=80, y=60, on_close_cb=None, on_body_click_cb=None,
                 wants_cursor=False):
        super().__init__(title, x, y, width, body_height)
        self.key = key
        self._draw_fn = draw_fn
        self._on_close_cb = on_close_cb
        self._on_body_click_cb = on_body_click_cb
        self.wants_cursor = bool(wants_cursor)

    def set_body_size(self, width, body_height):
        self.width = max(self.MIN_W, int(width))
        self._body_height = int(body_height)

    def draw_body(self, painter, x, y, w):
        try:
            self._draw_fn(painter, x, y, w, self.content_height())
        except Exception as exc:
            print(f"[CallbackWindow] draw_fn failed for '{self.key}': {exc}")

    def handle_body_click(self, x, y):
        if self._on_body_click_cb is not None:
            try:
                return bool(self._on_body_click_cb(x, y))
            except Exception as exc:
                print(f"[CallbackWindow] body click failed for '{self.key}': {exc}")
                return False
        return False

    def on_close(self):
        if self._on_close_cb is not None:
            try:
                self._on_close_cb()
            except Exception as exc:
                print(f"[CallbackWindow] on_close failed for '{self.key}': {exc}")
