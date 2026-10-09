"""The pause menu's floating window: SysMon's chrome around the menu's rows.

A :class:`~engine.floating_windows.FloatingWindow` -- title bar, drag,
collapse and [X] -- whose title is the current page ("Paused", "Options",
...) and whose body is that page's rows, set in the menu's font. The rows'
rectangles are stored back on the :class:`~engine.pause_menu.PauseMenu` for
mouse hits. [X] resumes play: the view watches ``active`` going False.
"""

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QBrush, QColor, QPen

from engine.floating_windows import FloatingWindow

_ORANGE = QColor(240, 128, 0)
_GREEN = QColor(120, 200, 80)


class PauseMenuWindow(FloatingWindow):
    ROW_H = 40
    GAP = 8
    PAD = 12
    NOTICE_H = 24
    WIDTH = 380

    def __init__(self, menu, font_for, x=40, y=40):
        """*font_for(pixel_size)* gives the menu's font at a size."""
        super().__init__(menu.page.title, x=x, y=y, width=self.WIDTH)
        self.menu = menu
        self._font_for = font_for
        self.title_font = font_for(15)

    def content_height(self):
        rows = len(self.menu.page.items)
        notice = self.NOTICE_H if self.menu.notice else 0
        return self.PAD * 2 + notice + rows * self.ROW_H + (rows - 1) * self.GAP

    def full_height(self):
        return self.HEADER_H + (self.content_height() if self.expanded else 0)

    def draw(self, painter, focused=True):
        self.title = self.menu.page.title
        if not self.expanded:
            self.menu.rects = []
        super().draw(painter, focused)

    def draw_body(self, painter, x, y, w):
        menu = self.menu
        painter.save()
        top = y + self.PAD
        if menu.notice:
            painter.setFont(self._font_for(15))
            painter.setPen(_GREEN)
            painter.drawText(QRect(x, top, w, self.NOTICE_H - 4), Qt.AlignCenter, menu.notice)
            top += self.NOTICE_H
        painter.setFont(self._font_for(19))
        row_w = w - 2 * self.PAD
        rects = []
        for index, item in enumerate(menu.page.items):
            row = QRect(x + self.PAD, top + index * (self.ROW_H + self.GAP), row_w, self.ROW_H)
            selected = index == menu.selected and item.enabled
            painter.setPen(QPen(_ORANGE if selected else QColor(110, 110, 110), 2))
            painter.setBrush(QBrush(QColor(70, 45, 20, 235) if selected else QColor(35, 35, 35, 220)))
            painter.drawRoundedRect(row, 5, 5)
            painter.setPen(QColor(255, 255, 255) if item.enabled else QColor(120, 120, 120))
            text = ("●  " + item.label) if item.checked else item.label
            bar = None
            if item.slider is not None:
                painter.drawText(row.adjusted(14, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
                bar = QRect(row.x() + row_w // 3, row.center().y() - 5, row_w // 2, 10)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(80, 80, 80)))
                painter.drawRect(bar)
                painter.setBrush(QBrush(_GREEN))
                painter.drawRect(QRect(bar.x(), bar.y(),
                                       int(bar.width() * item.slider_fraction()), bar.height()))
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(QRect(bar.right() + 4, row.y(), row.right() - bar.right() - 10,
                                       self.ROW_H),
                                 Qt.AlignVCenter | Qt.AlignRight, str(item.slider_value()))
            else:
                painter.drawText(row, Qt.AlignCenter, text)
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

