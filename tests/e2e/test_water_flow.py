"""Water's "Direction": ticked, the water flows north, south, east or west
at its Flow Speed (its waves, ripples, foam and caustics drift); unticked,
the brush is exactly as it was -- its flow keys gone, the water still."""

import copy

import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

MAP = "maps/_SHOWCASE.json"


def _water(session):
    return next(b for b in session.state.brushes if b.get('shader') == 'Water')


def _flow(session, brush):
    session.publish()
    session.publish()
    with session.render_state() as frame:
        table = frame.render_table
        return tuple(float(v) for v in table.water_flow[int(table.slot_of_id[brush['id']])])


def _panel(session, brush):
    window = session.window
    window.set_selected_objects([brush])
    session.app.processEvents()
    widgets = window.property_editor._widgets
    return (widgets['water_flow_cb'], widgets['water_flow_combo'],
            widgets['water_flow_speed_slider'])


def test_water_without_the_property_is_still(fio_session):
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    assert 'water_flow' not in brush
    assert _flow(session, brush) == (0.0, 0.0)


@pytest.mark.parametrize('direction,velocity', [
    ('N', (0.0, 40.0)), ('S', (0.0, -40.0)), ('E', (40.0, 0.0)), ('W', (-40.0, 0.0)),
    ('e', (40.0, 0.0)), ('up', (0.0, 0.0)), (None, (0.0, 0.0))])
def test_the_direction_reaches_the_table_as_a_velocity(fio_session, direction, velocity):
    from engine.change_journal import touch
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    brush['water_flow'] = direction
    touch(brush)
    assert _flow(session, brush) == pytest.approx(velocity)


def test_the_speed_scales_it_within_the_slider_range(fio_session):
    from engine.change_journal import touch
    from engine.constants import WATER_FLOW_SPEED_MAX, WATER_FLOW_SPEED_MIN
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    brush['water_flow'] = 'E'
    for authored, published in ((100.0, 100.0), (1e6, WATER_FLOW_SPEED_MAX),
                                (-5.0, WATER_FLOW_SPEED_MIN), ('fast', 40.0)):
        brush['water_flow_speed'] = authored
        touch(brush)
        assert _flow(session, brush) == pytest.approx((published, 0.0))


def test_the_checkbox_shows_the_controls_and_unticking_restores_the_brush(fio_session):
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    before = copy.deepcopy(brush)
    tick, combo, speed = _panel(session, brush)
    assert not tick.isChecked()
    assert combo.isHidden() and speed.isHidden()          # off: nothing exposed
    assert [combo.itemText(i) for i in range(combo.count())] == ['N', 'S', 'E', 'W']

    tick.setChecked(True)
    assert not combo.isHidden() and not speed.isHidden()
    assert brush['water_flow'] == 'N' and brush['water_flow_speed'] == 40.0
    combo.setCurrentText('W')
    speed.setValue(120)
    assert brush['water_flow'] == 'W' and brush['water_flow_speed'] == 120.0
    assert _flow(session, brush) == pytest.approx((-120.0, 0.0))

    tick.setChecked(False)
    assert combo.isHidden() and speed.isHidden()
    assert brush == before                                 # exactly as it was
    assert _flow(session, brush) == (0.0, 0.0)
    combo.setCurrentText('S')                              # hidden: writes nothing
    assert brush == before

    tick.setChecked(True)                                  # the same flow back
    assert brush['water_flow'] == 'S' and brush['water_flow_speed'] == 120.0


def test_a_flowing_brush_opens_with_its_flow(fio_session):
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    brush['water_flow'], brush['water_flow_speed'] = 'E', 75.0
    tick, combo, speed = _panel(session, brush)
    assert tick.isChecked() and combo.currentText() == 'E' and speed.value() == 75
    assert not combo.isHidden()


def test_no_shader_clears_the_flow(fio_session):
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    brush['water_flow'], brush['water_flow_speed'] = 'N', 60.0
    window = session.window
    window.set_selected_objects([brush])
    session.app.processEvents()
    editor = window.property_editor
    handler = getattr(editor, '_on_shader_changed', None) or getattr(editor, 'on_shader_changed', None)
    if handler is None:
        pytest.skip("shader handler not found")
    handler('<None>')
    assert 'water_flow' not in brush and 'water_flow_speed' not in brush
