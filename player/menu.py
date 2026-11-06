"""Shared pause-menu geometry, used for both drawing and hit testing.

The pause menu is a stack of pages. Each page is a title, a list of items and
a status line; :func:`layout` turns a page into screen rectangles, which the
renderer draws and :func:`hit_test` resolves taps against, so the two can
never disagree.

Item styles: ``button`` (default), ``selected`` (a highlighted choice),
``field`` (a text field; tap to type), ``label`` (inert text) and
``disabled`` (shown, not tappable).
"""
from dataclasses import dataclass, field
from typing import List, Tuple

TAPPABLE = ('button', 'selected', 'field')


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    style: str = 'button'


@dataclass(frozen=True)
class Page:
    title: str
    items: Tuple[Item, ...] = ()
    status: str = ''
    columns: int = 1


def root_page(has_world=True, extra=()):
    """The top pause page: Resume, any extra pages, Open world, Exit."""
    items = [Item('resume', 'Resume')] if has_world else []
    items += list(extra)
    items += [Item('open', 'Open world'), Item('exit', 'Exit')]
    return Page('Fio Player - Paused' if has_world else 'Fio Player', tuple(items))


def layout(page, width, height):
    """``[(key, label, (x, y, w, h), style)]`` for every item of *page*."""
    columns = max(1, page.columns)
    count = len(page.items)
    rows = max(1, -(-count // columns))
    gap = 14 if rows < 6 else 10
    top = height * (.30 if rows <= 4 else .17)
    bottom = height * .90
    button_height = max(36, min(72, height * .11, (bottom - top - gap * (rows - 1)) / rows))
    total = min(columns * 400 + (columns - 1) * 16, width * (.7 if columns == 1 else .92))
    button_width = (total - (columns - 1) * 16) / columns
    left = (width - total) / 2
    placed = []
    for index, item in enumerate(page.items):
        row, column = divmod(index, columns)
        rect = (left + column * (button_width + 16), top + row * (button_height + gap),
                button_width, button_height)
        placed.append((item.key, item.label, rect, item.style))
    return placed


def buttons(width, height, has_world=True):
    """The root page's buttons as ``(key, label, rect)`` (compatibility)."""
    return [(key, label, rect) for key, label, rect, _ in layout(root_page(has_world), width, height)]


def hit_test(position, width, height, has_world=True, page=None):
    """The key of the tappable item under *position*, or None."""
    page = page or root_page(has_world)
    x, y = position
    for key, _, (left, top, w, h), style in layout(page, width, height):
        if style in TAPPABLE and left <= x <= left + w and top <= y <= top + h:
            return key
    return None
