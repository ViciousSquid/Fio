"""The Brush tool in a 2D view draws; it does not pick brushes up.

A plain click on a brush starts drawing a new brush there (as on empty
space) and leaves the brush unselected; Shift+click selects it. Entities
are still selected with a plain click, and the Select tool is unchanged.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QPointF, Qt                          # noqa: E402

from editor.things import Light                               # noqa: E402
from tests.editor.test_view2d_component_interaction import (  # noqa: E402
    make_box, press, release,
)
from tests.helpers.worlds import make_thing                   # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def view(main_window):
    main_window.set_tool_mode('brush')
    v = main_window.view_top
    v.resize(800, 600)
    v.zoom_factor = 1.0
    v.pan_offset = QPointF(0.0, 0.0)
    v.grid_size = 16
    v.snap_to_grid_enabled = True
    return v


def _selected(main_window):
    return [o for o in main_window.state.selected_objects if o is not None]


def test_a_plain_click_on_a_brush_draws_instead_of_selecting(main_window, view):
    brush = make_box(pos=(0, 0, 0), size=(128, 128, 128))
    main_window.state.brushes.append(brush)
    press(view, (10, 10))
    assert _selected(main_window) == []
    assert view.is_drawing_brush and not view.is_dragging_object
    release(view, (10, 10))
    assert brush['pos'] == [0, 0, 0]                    # not moved either


def test_shift_click_selects_the_brush(main_window, view):
    brush = make_box(pos=(0, 0, 0), size=(128, 128, 128))
    main_window.state.brushes.append(brush)
    press(view, (10, 10), modifiers=Qt.ShiftModifier)
    release(view, (10, 10), modifiers=Qt.ShiftModifier)
    assert _selected(main_window) == [brush]
    assert not view.is_drawing_brush


def test_entities_are_still_selected_with_a_plain_click(main_window, view):
    lamp = make_thing(Light, "lamp", (300.0, 0.0, 300.0))
    main_window.state.things.append(lamp)
    ax1, ax2 = view.get_axes()
    index = {'x': 0, 'y': 1, 'z': 2}
    point = (lamp.pos[index[ax1]], lamp.pos[index[ax2]])
    press(view, point)
    release(view, point)
    assert _selected(main_window) == [lamp]


def test_the_select_tool_still_selects_with_a_plain_click(main_window, view):
    main_window.set_tool_mode('select')
    brush = make_box(pos=(0, 0, 0), size=(128, 128, 128))
    main_window.state.brushes.append(brush)
    press(view, (10, 10))
    release(view, (10, 10))
    assert _selected(main_window) == [brush]
