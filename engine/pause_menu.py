"""The pause menu's pages and cursor (Esc in Play Mode).

Qt-free: QtGameView paints a ``PauseMenu`` and feeds it keys and clicks; the
items' actions do the work (save a slot, set the volume, ...). A page is a
title and its items; an item either runs an action, opens a sub-page, or is a
slider. Every sub-page ends with Back, and Esc goes back one page -- from the
first page it closes the menu.
"""


class Item:
    """One row of a page.

    *label* is text or a zero-argument callable (a save slot's label names
    when it was saved, so it is read each time the page is drawn).
    *action* runs on activation; *page* is a zero-argument callable building
    the sub-page to open; *slider* is ``(get, set, low, high, step)`` for a
    value changed with Left/Right or a click on the bar. *enabled* and
    *checked* are values or zero-argument callables; a checked item is drawn
    marked (the current choice of a set).
    """

    def __init__(self, label, action=None, *, page=None, slider=None,
                 enabled=True, checked=False):
        self._label = label
        self.action = action
        self.page = page
        self.slider = slider
        self._enabled = enabled
        self._checked = checked

    @staticmethod
    def _value(v):
        return v() if callable(v) else v

    @property
    def label(self) -> str:
        return str(self._value(self._label))

    @property
    def enabled(self) -> bool:
        return bool(self._value(self._enabled))

    @property
    def checked(self) -> bool:
        return bool(self._value(self._checked))

    # -- slider ---------------------------------------------------------------

    def slider_value(self):
        get, _set, low, high, _step = self.slider
        return max(low, min(high, get()))

    def slider_fraction(self) -> float:
        _get, _set, low, high, _step = self.slider
        return (self.slider_value() - low) / float(high - low) if high > low else 0.0

    def set_slider_value(self, value):
        _get, set_, low, high, _step = self.slider
        set_(max(low, min(high, value)))

    def set_slider_fraction(self, fraction):
        _get, _set, low, high, step = self.slider
        fraction = max(0.0, min(1.0, float(fraction)))
        value = low + fraction * (high - low)
        self.set_slider_value(int(round(value / step)) * step)


class Page:
    def __init__(self, title, items):
        self.title = title
        self.items = list(items)


class PauseMenu:
    """A stack of pages with a selected row on each."""

    def __init__(self, root: Page):
        self._stack = [[root, 0]]
        self._skip_disabled(1)
        #: Screen rectangles of the rows and slider bars, set by whoever
        #: paints the menu, for mouse hits: ``[(row_rect, bar_rect_or_None)]``.
        self.rects = []
        #: One line under the title ("Saved to Slot 1"), cleared on page change.
        self.notice = ""

    # -- state ----------------------------------------------------------------

    @property
    def page(self) -> Page:
        return self._stack[-1][0]

    @property
    def selected(self) -> int:
        return self._stack[-1][1]

    @selected.setter
    def selected(self, index):
        if 0 <= index < len(self.page.items):
            self._stack[-1][1] = index

    @property
    def depth(self) -> int:
        return len(self._stack)

    @property
    def item(self) -> Item:
        return self.page.items[self.selected]

    # -- navigation ------------------------------------------------------------

    def _skip_disabled(self, step):
        items = self.page.items
        for _ in range(len(items)):
            if items[self.selected].enabled:
                return
            self._stack[-1][1] = (self.selected + step) % len(items)

    def move(self, step):
        items = self.page.items
        index = self.selected
        for _ in range(len(items)):
            index = (index + step) % len(items)
            if items[index].enabled:
                self._stack[-1][1] = index
                return

    def push(self, page: Page):
        page.items.append(Item("Back", self.back))
        self._stack.append([page, 0])
        self._skip_disabled(1)
        self.rects = []
        self.notice = ""

    def back(self) -> bool:
        """Go up one page. False on the first page (the caller closes)."""
        if len(self._stack) == 1:
            return False
        self._stack.pop()
        self.rects = []
        self.notice = ""
        return True

    def activate(self, index=None):
        """Run the selected (or *index*) row: its action, or open its page."""
        if index is not None:
            self.selected = index
        item = self.item
        if not item.enabled:
            return
        if item.page is not None:
            self.push(item.page())
        elif item.action is not None:
            item.action()

    def adjust(self, direction):
        """Left/Right: step the selected slider. True if there was one."""
        item = self.item
        if item.slider is None or not item.enabled:
            return False
        step = item.slider[4]
        item.set_slider_value(item.slider_value() + step * direction)
        return True

    def hit(self, x, y):
        """The row index under (x, y), and the bar fraction if on a slider."""
        for index, (row, bar) in enumerate(self.rects):
            if row.contains(x, y):
                fraction = None
                if bar is not None and bar.contains(x, y) and bar.width() > 0:
                    fraction = (x - bar.x()) / float(bar.width())
                return index, fraction
        return None, None
