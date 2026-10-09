"""The pause menu's pages and cursor, without Qt."""

from engine.pause_menu import Item, Page, PauseMenu


class Rect:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def x(self):
        return self._x

    def width(self):
        return self._w

    def contains(self, x, y):
        return self._x <= x < self._x + self._w and self._y <= y < self._y + self._h


def labels(menu):
    return [item.label for item in menu.page.items]


def test_up_and_down_wrap_and_skip_what_cannot_be_chosen():
    menu = PauseMenu(Page("Paused", [Item("a"), Item("b", enabled=False), Item("c")]))
    assert menu.selected == 0
    menu.move(1)
    assert menu.item.label == "c"
    menu.move(1)
    assert menu.item.label == "a"
    menu.move(-1)
    assert menu.item.label == "c"


def test_a_sub_page_ends_with_back_and_esc_goes_back_then_closes():
    ran = []
    root = Page("Paused", [Item("Resume", lambda: ran.append("resume")),
                           Item("Options", page=lambda: Page("Options", [Item("Video")]))])
    menu = PauseMenu(root)
    menu.activate(1)
    assert menu.page.title == "Options" and labels(menu) == ["Video", "Back"]
    menu.activate(1)                                  # Back
    assert menu.page.title == "Paused" and menu.selected == 1
    menu.activate(1)
    assert menu.back() is True
    assert menu.back() is False                       # first page: the caller closes
    menu.activate(0)
    assert ran == ["resume"]


def test_a_page_whose_rows_are_all_disabled_starts_on_back():
    menu = PauseMenu(Page("Paused", [Item("Load", page=lambda: Page(
        "Load", [Item("Slot 1", enabled=False), Item("Slot 2", enabled=False)]))]))
    menu.activate()
    assert menu.item.label == "Back"
    menu.activate(0)                                  # disabled: nothing happens
    assert menu.page.title == "Load"


def test_labels_and_marks_are_read_when_drawn():
    state = {"mode": "Fullscreen"}
    item = Item(lambda: state["mode"], checked=lambda: state["mode"] == "Windowed")
    assert item.label == "Fullscreen" and not item.checked
    state["mode"] = "Windowed"
    assert item.label == "Windowed" and item.checked


def test_a_slider_steps_clamps_and_takes_a_click_on_its_bar():
    volume = {"v": 50}
    slider = Item("Volume", slider=(lambda: volume["v"],
                                    lambda v: volume.__setitem__("v", v), 0, 100, 5))
    menu = PauseMenu(Page("Options", [slider, Item("Video")]))
    assert menu.adjust(1) and volume["v"] == 55
    for _ in range(20):
        menu.adjust(1)
    assert volume["v"] == 100
    menu.rects = [(Rect(0, 0, 400, 40), Rect(100, 14, 200, 12)), (Rect(0, 50, 400, 40), None)]
    index, fraction = menu.hit(150, 20)
    assert index == 0 and fraction == 0.25
    slider.set_slider_fraction(fraction)
    assert volume["v"] == 25
    assert menu.hit(10, 60) == (1, None)
    assert menu.hit(10, 45) == (None, None)
    menu.move(1)
    assert not menu.adjust(1)                         # Video is not a slider
