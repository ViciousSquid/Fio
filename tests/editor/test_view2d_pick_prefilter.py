"""A click in a 2D view tests only the brushes that may hold the point.

get_object_at asked every brush in the map for its derived bounds on every
click. The brush cull's bounds array (View2D._brush_cull_index) now picks
the brushes that may hold the point, and the exact test decides as before:
whatever is picked, and in which order, is what testing every brush gave.
"""

import random

import pytest

pytest.importorskip("PyQt5", reason="the 2D views are Qt widgets")

from PyQt5.QtCore import QPointF                    # noqa: E402

from tests.helpers.worlds import box_brush         # noqa: E402

pytestmark = pytest.mark.qt


def _every_row(view):
    return lambda world_pos, i1, i2: list(range(len(view.editor.state.brushes)))


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
        m.setattr(view, "_brush_rows_at", _every_row(view))
        moved['pos'][0] -= 200.0
        main_window.state.selected_objects = []
        slow = picks()
        main_window.state.selected_objects = [moved]
        moved['pos'][0] += 200.0
        slow_moved = picks()

    assert any(p is not None for p in slow)
    assert all(a is b for a, b in zip(fast, slow)) and len(fast) == len(slow)
    assert all(a is b for a, b in zip(fast_moved, slow_moved))
