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


def _probes(host):
    from editor.things import ReflectionProbe
    return [t for t in host.state.things if isinstance(t, ReflectionProbe)]


def test_switching_it_on_places_a_probe_over_the_water_and_off_deletes_it(main_window):
    """High quality on gives the water a reflection probe above its surface;
    off deletes that probe -- never one the author placed themselves."""
    from editor.things import ReflectionProbe, WATER_PROBE_HEIGHT, water_probe_of

    host, editor = _panel(main_window, water_high_quality=False)
    placed = ReflectionProbe(pos=[900.0, 200.0, 900.0])
    host.state.things.append(placed)
    box = editor._widgets['water_quality_cb']

    box.setChecked(True)
    brush = host.state.brushes[0]
    probe = water_probe_of(brush, host.state.things)
    assert brush['water_high_quality'] is True and probe is not None
    assert probe.pos == [0.0, 32.0 + WATER_PROBE_HEIGHT, 0.0]     # over the top surface
    assert probe.get_radius() >= 512.0
    assert len(_probes(host)) == 2

    box.setChecked(False)
    assert brush['water_high_quality'] is False
    assert water_probe_of(brush, host.state.things) is None
    assert _probes(host) == [placed]


def test_the_flag_and_its_probe_are_one_undo_step(main_window):
    from editor.things import water_probe_of

    host, editor = _panel(main_window, water_high_quality=False)
    editor._widgets['water_quality_cb'].setChecked(True)
    assert water_probe_of(host.state.brushes[0], host.state.things) is not None
    host.undo()
    brush = host.state.brushes[0]
    assert brush.get('water_high_quality') is False
    assert water_probe_of(brush, host.state.things) is None


def test_ticking_it_again_never_makes_a_second_probe(main_window):
    from editor.things import water_probe_of

    host, editor = _panel(main_window, water_high_quality=False)
    box = editor._widgets['water_quality_cb']
    box.setChecked(True)
    first = water_probe_of(host.state.brushes[0], host.state.things)
    editor._set_water_high_quality(True)        # already on: nothing more
    assert _probes(host) == [first]
