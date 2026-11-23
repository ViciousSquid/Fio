"""A model stays drawn in a 2D view however far it is zoomed in.

The view culled an entity by its origin alone, padded by 32 *pixels*. A
model's origin is the point it stands on: zoomed in on its top, the origin
left the view and the whole model vanished -- the closer the zoom, the
smaller that margin in world units, so it happened exactly when editing
finely.
"""

import numpy as np
import pytest
from PyQt5.QtCore import QPointF

from editor import model_wireframe as mw
from tests.editor.test_model_wireframe import Flat, _cube

pytestmark = pytest.mark.qt

TALL = (32.0, 200.0, 32.0)        # a column 200 units tall on its origin


def _setup(main_window, monkeypatch, zoom, pan):
    from editor.things import Prop
    positions, edges = mw.build_edges(_cube() * np.array(TALL), Flat())
    view = main_window.view_front                     # X across, Y up
    monkeypatch.setattr(view, '_model_edges_for',
                        lambda thing: (object(), positions, edges))
    thing = Prop.for_model('assets/models/column.obj', pos=[0.0, 0.0, 0.0])
    main_window.state.things[:] = [thing]
    main_window.state.brushes[:] = []
    view.resize(400, 300)
    view.zoom_factor = zoom
    view.pan_offset = QPointF(*pan)
    return view, thing


@pytest.mark.parametrize("zoom", [4.0, 16.0, 64.0])
def test_zoomed_in_on_a_models_top_it_is_still_drawn(main_window, monkeypatch, zoom):
    # Looking at the top of the column: its origin (at its foot) is far off
    # the bottom of the view.
    view, thing = _setup(main_window, monkeypatch, zoom, (16.0, 190.0))
    bounds = view.get_visible_world_bounds()
    assert not bounds.contains(QPointF(0.0, 0.0))         # the origin is out of sight
    assert view.is_thing_visible(thing, bounds, 0, 1)
    drawn = []
    original = view._draw_model_wireframe
    monkeypatch.setattr(view, '_draw_model_wireframe',
                        lambda painter, t, *a: drawn.append(t) or original(painter, t, *a))
    view.grab()                                            # a real paint
    assert drawn == [thing]


def test_a_model_wholly_out_of_sight_is_still_culled(main_window, monkeypatch):
    view, thing = _setup(main_window, monkeypatch, 16.0, (16.0, 600.0))
    assert not view.is_thing_visible(thing, view.get_visible_world_bounds(), 0, 1)


def test_zoomed_in_on_a_models_top_a_click_there_selects_it(main_window, monkeypatch):
    view, thing = _setup(main_window, monkeypatch, 16.0, (16.0, 190.0))
    centre = view.world_to_screen(QPointF(16.0, 190.0))
    assert view.get_object_at(centre) is thing


def test_zoomed_in_on_a_lights_radius_circle_it_is_still_drawn(main_window):
    """A radius is world units: it was divided by the zoom as pixels are,
    so zoomed in on a radius circle away from its light the light (and the
    circle) were culled."""
    from editor.things import Light
    light = Light(pos=[0.0, 0.0, 0.0])
    light.properties['radius'] = 300.0
    light.properties['show_radius'] = True
    view = main_window.view_front
    view.resize(400, 300)
    view.zoom_factor = 16.0
    view.pan_offset = QPointF(290.0, 0.0)           # on the circle's edge
    assert light.get_radius() == pytest.approx(300.0)
    assert view.is_thing_visible(light, view.get_visible_world_bounds(), 0, 1)
