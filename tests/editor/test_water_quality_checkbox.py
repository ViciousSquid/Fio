"""The water shader tab's "High quality" checkbox.

Water quality is a property of each water brush (``water_high_quality``,
default on), not a renderer or settings.ini setting. The Property Editor
shows it at the top of a water brush's shader properties, next to "Draw top
surface only".
"""

import configparser
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QCheckBox  # noqa: E402

from engine import brush_geometry as bg  # noqa: E402

pytestmark = pytest.mark.qt


def water_brush():
    return {
        'pos': [0, 0, 0], 'size': [256, 64, 256], 'name': 'pool', 'id': 'id-pool',
        'textures': {tag: 'water.jpg' for tag in bg.FACE_TAGS},
        'shader': 'Water', 'is_water': True,
    }


def _panel(main_window, **props):
    host = main_window
    editor = host.property_editor
    brush = water_brush()
    brush.update(props)
    host.state.brushes = [brush]
    host.set_selected_objects([brush])
    editor.set_object(brush, force=True)
    return host, editor


def test_the_checkbox_sits_at_the_top_beside_top_surface_only(main_window):
    host, editor = _panel(main_window)
    quality = editor._widgets['water_quality_cb']
    plane = editor._widgets['water_plane_cb']
    assert quality.text() == "High quality"
    assert quality.parentWidget() is plane.parentWidget()
    # Same row, quality first, above every other water control.
    checkboxes = [w for w in quality.parentWidget().findChildren(QCheckBox)
                  if w.text() in ("High quality", "Draw top surface only", "Enable Waves")]
    order = sorted(checkboxes, key=lambda w: (w.mapTo(quality.window(), w.rect().topLeft()).y(),
                                              w.mapTo(quality.window(), w.rect().topLeft()).x()))
    assert [w.text() for w in order][:2] == ["High quality", "Draw top surface only"]
    y = [w.mapTo(quality.window(), w.rect().center()).y() for w in order[:2]]
    assert abs(y[0] - y[1]) <= 2                # one row


@pytest.mark.parametrize("props, checked", [
    ({}, True),                                  # default on
    ({'water_high_quality': True}, True),
    ({'water_high_quality': False}, False),
    ({'water_high_quality': 'false'}, False),    # hand-edited map
])
def test_the_checkbox_shows_the_brush_setting(main_window, props, checked):
    host, editor = _panel(main_window, **props)
    assert editor._widgets['water_quality_cb'].isChecked() == checked


@pytest.mark.gl
def test_ticking_it_sets_only_this_brush_and_never_the_renderer(main_window):
    host, editor = _panel(main_window)
    box = editor._widgets['water_quality_cb']
    box.setChecked(False)
    assert host.state.brushes[0]['water_high_quality'] is False
    box.setChecked(True)
    assert host.state.brushes[0]['water_high_quality'] is True
    # A brush property, never a renderer or settings.ini setting.
    assert host.view_3d.renderer.water_quality == 'expensive'
    assert not host.config.has_option('Renderer', 'water_quality')


def test_the_render_table_reads_the_flag_per_brush():
    from engine.render_table import RenderTable, CLASS_WATER

    hq = water_brush()
    cheap = dict(water_brush(), id='id-cheap', water_high_quality=False)
    table = RenderTable()
    table.begin_frame([hq, cheap])
    assert (table.class_bits[:2] & CLASS_WATER).all()
    assert table.water_high_quality[:2].tolist() == [True, False]
