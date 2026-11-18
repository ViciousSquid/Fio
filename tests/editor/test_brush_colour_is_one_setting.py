"""A brush has one colour, whichever control sets it.

The Asset Browser's Tint button, Appearance > Brush Colour and a Glow brush's
Glow Color are the same setting -- the brush's ``colour`` -- so an emissive
Glow brush glows, and lights its surroundings, in the colour any of them
chose. (Glow Color used to write ``glow_color``, which nothing read: two
colour settings, one of them doing nothing.)
"""
import pytest
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QColorDialog

pytestmark = pytest.mark.qt


def _glow_brush(main_window):
    brush = {'id': 'b-glow', 'name': 'neon', 'pos': [0, 0, 0], 'size': [64, 64, 64],
             'shader': 'Glow', 'glow_intensity': 3.0, 'emissive': True,
             'emissive_intensity': 2.0, 'emissive_radius': 300.0}
    main_window.state.brushes.append(brush)
    main_window.set_selected_objects([brush])
    main_window.property_editor.set_object(brush)
    return brush


def _choose(monkeypatch, rgb):
    monkeypatch.setattr(QColorDialog, 'getColor',
                        staticmethod(lambda *a, **k: QColor(*rgb)))


def _rgb(colour):
    return [round(c * 255) for c in colour]


def test_the_asset_browser_tint_is_the_glow_colour(main_window, monkeypatch):
    brush = _glow_brush(main_window)
    _choose(monkeypatch, (255, 0, 128))
    main_window.tint_selected_brush()                    # Asset Browser > Tint
    assert _rgb(brush['colour']) == [255, 0, 128]
    assert 'glow_color' not in brush
    assert _rgb(main_window.brush_colour(brush)) == [255, 0, 128]


def test_the_glow_color_button_sets_the_brush_colour(main_window, monkeypatch):
    brush = _glow_brush(main_window)
    brush['tint'] = [0, 255, 0]                          # left from the console
    _choose(monkeypatch, (0, 64, 255))
    main_window.property_editor._pick_brush_colour(
        main_window.property_editor._widgets['glow_color_btn'])
    assert _rgb(brush['colour']) == [0, 64, 255]
    assert 'tint' not in brush and 'glow_color' not in brush


def test_what_it_glows_and_lights_with_is_that_colour(main_window, monkeypatch):
    import numpy as np
    from engine.render_table import RenderTable
    brush = _glow_brush(main_window)
    _choose(monkeypatch, (255, 128, 0))
    main_window.tint_selected_brush()
    table = RenderTable()
    table.sync([brush])
    slot = int(table.slot_of_id[brush['id']])
    assert np.allclose(table.colour[slot], [1.0, 128 / 255, 0.0], atol=1e-3)
    glow = table.glow_colour[slot]
    assert glow[0] > glow[1] > glow[2] == 0.0              # glows in it
    emit_colour, _emit = table._emissive_of[id(brush)]
    assert np.allclose(emit_colour, [1.0, 128 / 255, 0.0], atol=1e-3)   # lights in it


def test_appearance_brush_colour_is_the_same_setting(main_window, monkeypatch):
    brush = _glow_brush(main_window)
    editor = main_window.property_editor
    _choose(monkeypatch, (10, 200, 30))
    editor._pick_brush_colour(editor._widgets['brush_colour_btn'])
    assert _rgb(brush['colour']) == [10, 200, 30]
    main_window.property_editor.set_object(brush)
    assert _rgb(main_window.brush_colour(brush)) == [10, 200, 30]
