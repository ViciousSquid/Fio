"""The pause menu's floating window: SysMon's chrome around the menu's rows.

A :class:`~engine.floating_windows.FloatingWindow` -- title bar, drag,
collapse and [X] -- whose title is the current page ("Paused", "Options",
...) and whose body is that page's rows, set in the menu's font. The rows'
rectangles are stored back on the :class:`~engine.pause_menu.PauseMenu` for
mouse hits. [X] resumes play: the view watches ``active`` going False.
"""

from PyQt5.QtCore import Qt, QPointF, QRect
from PyQt5.QtGui import QBrush, QColor, QFontMetricsF, QPainter, QPainterPath, QPen

from engine.floating_windows import FloatingWindow

_ORANGE = QColor(240, 128, 0)
_GREEN = QColor(120, 200, 80)


class PauseMenuWindow(FloatingWindow):
    ROW_H = 44
    GAP = 8
    PAD = 12
    NOTICE_H = 26
    WIDTH = 380

    #: Pixel sizes of the row, notice and title text. The display font's
    #: strokes are a pixel wide, so at the old 19/15 px the menu was hard to
    #: read in a small 3D view.
    ROW_FONT_PX = 22
    NOTICE_FONT_PX = 17
    TITLE_FONT_PX = 17
    #: Pen width traced round each glyph, thickening those hairline strokes.
    TEXT_WEIGHT = 1.0

    def __init__(self, menu, font_for, x=40, y=40):
        """*font_for(pixel_size)* gives the menu's font at a size."""
        super().__init__(menu.page.title, x=x, y=y, width=self.WIDTH)
        self.menu = menu
        self._font_for = font_for
        self.title_font = font_for(self.TITLE_FONT_PX)

    def content_height(self):
        rows = len(self.menu.page.items)
        notice = self.NOTICE_H if self.menu.notice else 0
        return self.PAD * 2 + notice + rows * self.ROW_H + (rows - 1) * self.GAP

    def full_height(self):
        return self.HEADER_H + (self.content_height() if self.expanded else 0)

    def draw(self, painter, focused=True):
        if not self.expanded:
            self.menu.rects = []
        # The chrome draws the bar, the arrow and [X]; the title is drawn here,
        # traced like the rows, so it reads as well as they do.
        self.title = ""
        super().draw(painter, focused)
        self.title = self.menu.page.title
        rect = self.window_rect
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._text(painter, QRect(rect.x() + 10, rect.y(), rect.width() - 70, self.HEADER_H),
                   Qt.AlignLeft, self.title, self.TITLE_FONT_PX, QColor(255, 255, 255))
        painter.restore()

    def _text(self, painter, rect, align, text, px, color):
        """*text* in *rect*, aligned left, right or centred and always centred
        vertically, filled and traced with a TEXT_WEIGHT pen."""
        font = self._font_for(px)
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text)
        if align & Qt.AlignLeft:
            left = rect.left()
        elif align & Qt.AlignRight:
            left = rect.right() + 1 - width
        else:
            left = rect.left() + (rect.width() - width) / 2.0
        baseline = rect.top() + (rect.height() + metrics.ascent() - metrics.descent()) / 2.0
        path = QPainterPath()
        path.addText(QPointF(left, baseline), font, text)
        painter.setPen(QPen(color, self.TEXT_WEIGHT))
        painter.setBrush(QBrush(color))
        painter.drawPath(path)

    def draw_body(self, painter, x, y, w):
        menu = self.menu
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        top = y + self.PAD
        if menu.notice:
            self._text(painter, QRect(x, top, w, self.NOTICE_H - 4), Qt.AlignCenter,
                       menu.notice, self.NOTICE_FONT_PX, _GREEN)
            top += self.NOTICE_H
        row_w = w - 2 * self.PAD
        rects = []
        for index, item in enumerate(menu.page.items):
            row = QRect(x + self.PAD, top + index * (self.ROW_H + self.GAP), row_w, self.ROW_H)
            selected = index == menu.selected and item.enabled
            painter.setPen(QPen(_ORANGE if selected else QColor(110, 110, 110), 2))
            painter.setBrush(QBrush(QColor(70, 45, 20, 235) if selected else QColor(35, 35, 35, 220)))
            painter.drawRoundedRect(row, 5, 5)
            colour = QColor(255, 255, 255) if item.enabled else QColor(120, 120, 120)
            text = ("●  " + item.label) if item.checked else item.label
            bar = None
            if item.slider is not None:
                self._text(painter, row.adjusted(14, 0, 0, 0), Qt.AlignLeft, text,
                           self.ROW_FONT_PX, colour)
                bar = QRect(row.x() + row_w // 3, row.center().y() - 5, row_w // 2, 10)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(80, 80, 80)))
                painter.drawRect(bar)
                painter.setBrush(QBrush(_GREEN))
                painter.drawRect(QRect(bar.x(), bar.y(),
                                       int(bar.width() * item.slider_fraction()), bar.height()))
                self._text(painter, QRect(bar.right() + 4, row.y(),
                                          row.right() - bar.right() - 10, self.ROW_H),
                           Qt.AlignRight, str(item.slider_value()), self.ROW_FONT_PX,
                           QColor(255, 255, 255))
            else:
                self._text(painter, row, Qt.AlignCenter, text, self.ROW_FONT_PX, colour)
            rects.append((row, bar))
        menu.rects = rects
        painter.restore()

    def handle_body_click(self, x, y):
        return False          # the view routes body clicks to the menu itself

    def centre_in(self, view_w, view_h):
        self.window_rect.moveTo(max(5, (view_w - self.width) // 2),
                                max(5, (view_h - self.full_height()) // 2))

    def clamp_to(self, view_w, view_h):
        x = max(5, min(self.window_rect.x(), max(5, view_w - self.width - 5)))
        y = max(5, min(self.window_rect.y(), max(5, view_h - self.HEADER_H - 5)))
        self.window_rect.moveTo(x, y)

