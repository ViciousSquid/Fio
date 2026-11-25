"""A click or a marquee in a 2D view tests only the brushes that may meet it.

get_object_at and the marquee's _objects_in_rect asked every brush in the
map for its derived bounds on every click and every mouse move. The brush
cull's bounds array (View2D._brush_cull_index) now picks the brushes that
may meet the point or rectangle, and the exact test decides as before:
whatever is picked, and in which order, is what testing every brush gave.
"""

import random

import pytest

pytest.importorskip("PyQt5", reason="the 2D views are Qt widgets")

from PyQt5.QtCore import QPointF                    # noqa: E402

from tests.helpers.worlds import box_brush         # noqa: E402

pytestmark = pytest.mark.qt


def _every_row(view):
    return lambda *args: list(range(len(view.editor.state.brushes)))


def test_a_click_picks_what_testing_every_brush_picked(main_window, monkeypatch):
    rng = random.Random(7)
    brushes = [box_brush(f"b{i}", (rng.uniform(-600, 600), 0, rng.uniform(-600, 600)),
                         (rng.choice([0.0, 16.0, 64.0, 300.0]), 64.0,
                          rng.choice([0.0, 16.0, 64.0, 300.0])))
               for i in range(120)]
    # Touching edges, a brush on a grid line, nested boxes.
    brushes += [box_brush("edge", (0, 0, 0), (128, 64, 128)),
                box_brush("inner", (0, 0, 0), (32, 64, 32))]
    main_window.state.brushes[:] = brushes
    main_window.state.things[:] = []
    view = main_window.view_top
    view.view_type = 'top'
    view.resize(400, 300)
    view.zoom_factor = 0.4
    view.pan_offset = QPointF(0.0, 0.0)

    def picks():
        return [view.get_object_at(QPointF(x, y), cycle=False)
                for x in range(0, 400, 7) for y in range(0, 300, 7)]

    fast = picks()
    moved = brushes[5]
    main_window.state.selected_objects = [moved]
    moved['pos'][0] += 200.0                         # written in place, as a drag does
    fast_moved = picks()

    with monkeypatch.context() as m:
        m.setattr(view, "_brush_rows_touching", _every_row(view))
        moved['pos'][0] -= 200.0
        main_window.state.selected_objects = []
        slow = picks()
        main_window.state.selected_objects = [moved]
        moved['pos'][0] += 200.0
        slow_moved = picks()

    assert any(p is not None for p in slow)
    assert all(a is b for a, b in zip(fast, slow)) and len(fast) == len(slow)
    assert all(a is b for a, b in zip(fast_moved, slow_moved))


def test_a_marquee_selects_what_testing_every_brush_selected(main_window, monkeypatch):
    from PyQt5.QtCore import QRectF
    rng = random.Random(11)
    brushes = [box_brush(f"m{i}", (rng.uniform(-600, 600), 0, rng.uniform(-600, 600)),
                         (rng.choice([0.0, 16.0, 64.0, 300.0]), 64.0,
                          rng.choice([0.0, 16.0, 64.0, 300.0])))
               for i in range(150)]
    brushes.append(box_brush("touching", (100, 0, 0), (100, 64, 100)))  # x 50..150
    main_window.state.brushes[:] = brushes
    main_window.state.things[:] = []
    view = main_window.view_top
    view.view_type = 'top'
    rects = [QRectF(rng.uniform(-700, 700), rng.uniform(-700, 700),
                    rng.uniform(-400, 400), rng.uniform(-400, 400)) for _ in range(60)]
    rects += [QRectF(150, -10, 50, 20), QRectF(0, 0, 0, 0), QRectF(50, -50, 100, 100)]

    def selections():
        return [view._objects_in_rect(r, enclose=e) for r in rects for e in (False, True)]

    fast = selections()
    with monkeypatch.context() as m:
        m.setattr(view, "_brush_rows_touching", _every_row(view))
        slow = selections()
    assert any(slow)
    assert [[id(o) for o in a] for a in fast] == [[id(o) for o in b] for b in slow]


def _painted(view):
    from PyQt5.QtGui import QImage, QPainter
    image = QImage(view.width(), view.height(), QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    view.draw_things(painter, view.get_visible_world_bounds())
    painter.end()
    return bytes(image.constBits().asarray(image.sizeInBytes()))


def test_draw_things_draws_what_testing_every_thing_drew(main_window, monkeypatch):
    """draw_things picks the things that may be in view by where they are
    now; whatever it draws is what testing every thing drew -- moved with no
    edit (as play moves them), a light's radius changed, zoomed and panned."""
    from editor.things import Light, Monster, PlayerStart, Speaker
    from tests.helpers.worlds import make_thing
    rng = random.Random(3)
    things = [make_thing(Monster, f"mon{i}", (rng.uniform(-900, 900), 0, rng.uniform(-900, 900)))
              for i in range(80)]
    lamp = make_thing(Light, "lamp", (700.0, 64.0, 0.0), radius=150.0)
    things += [lamp, make_thing(Speaker, "speaker", (-650.0, 0.0, 300.0)),
               make_thing(PlayerStart, "start", (0.0, 0.0, -700.0))]
    main_window.state.brushes[:] = []
    main_window.state.things[:] = things
    view = main_window.view_top
    view.view_type = 'top'
    view.resize(320, 240)

    def frames():
        out = []
        for zoom in (0.1, 0.4, 1.5):
            for pan in ((0.0, 0.0), (650.0, 0.0), (-500.0, 400.0)):
                view.zoom_factor = zoom
                view.pan_offset = QPointF(*pan)
                out.append(_painted(view))
        return out

    def every_thing(*args):
        return list(view.editor.state.things)

    def compare():
        fast = frames()
        with monkeypatch.context() as m:
            m.setattr(view, "_things_in_view", every_thing)
            slow = frames()
        assert any(any(frame) for frame in slow)       # things were drawn
        assert fast == slow

    compare()
    for thing in things[:40]:
        thing.pos[0] += 333.0                          # moved, no edit
    lamp.properties["radius"] = 900.0                  # a torch flickering
    compare()
