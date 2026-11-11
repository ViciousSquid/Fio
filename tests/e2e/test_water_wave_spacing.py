"""Water's "Wave Spacing": a slider from the tight, choppy waves (left, the
earlier look) to the broad swell (right, the default)."""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

MAP = "maps/_SHOWCASE.json"


def _water(session):
    return next(b for b in session.state.brushes if b.get('shader') == 'Water')


def _slot(session, brush):
    with session.render_state() as frame:
        table = frame.render_table
        return table, int(table.slot_of_id[brush['id']])


def test_a_map_without_the_property_gets_the_broad_waves(fio_session):
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    assert 'water_wave_spread' not in brush
    session.publish()
    table, slot = _slot(session, brush)
    assert table.water_wave_spread[slot] == 1.0


def test_the_property_reaches_the_table_clamped(fio_session):
    from engine.change_journal import touch
    session = fio_session(MAP, size=(200, 160))
    brush = _water(session)
    for authored, published in ((0.0, 0.0), (0.35, 0.35), (7.0, 1.0), (-2.0, 0.0)):
        brush['water_wave_spread'] = authored
        touch(brush)
        session.publish()
        session.publish()
        table, slot = _slot(session, brush)
        assert table.water_wave_spread[slot] == pytest.approx(published)


def test_the_slider_is_in_the_water_properties_and_writes_the_brush(fio_session):
    session = fio_session(MAP, size=(200, 160))
    window, brush = session.window, _water(session)
    window.set_selected_objects([brush])
    session.app.processEvents()
    slider = window.property_editor._widgets.get('water_wave_spread_slider')
    assert slider is not None, "no Wave Spacing slider for a water brush"
    assert slider.value() == 100                    # fully right by default
    slider.setValue(0)
    session.app.processEvents()
    assert brush['water_wave_spread'] == 0.0
    slider.setValue(60)
    assert brush['water_wave_spread'] == pytest.approx(0.6)


def test_the_two_ends_look_different(fio_session):
    import glm
    import math
    from engine.change_journal import touch
    session = fio_session(MAP, size=(240, 200))
    view = session.view
    if view.renderer is None or not view.renderer.ready:
        pytest.skip("no OpenGL context")
    view.sysmon.set_active(False)
    eye, target = glm.vec3(620.0, 210.0, 200.0), glm.vec3(960.0, 60.0, -192.0)
    d = glm.normalize(target - eye)
    view.set_editor_camera(eye, math.degrees(math.atan2(d.z, d.x)),
                           math.degrees(math.asin(d.y)))
    brush = _water(session)

    def frame(spread):
        brush['water_wave_spread'] = spread
        touch(brush)
        for _ in range(3):
            session.publish()
        return session.paint().astype(int)

    broad, tight = frame(1.0), frame(0.0)
    water = slice(broad.shape[0] // 2, None)
    assert np.abs(broad[water] - tight[water]).mean() > 2.0
    # Tight waves put more fine detail on the surface than the broad swell.
    detail = lambda image: np.abs(np.diff(image[water].sum(axis=2), axis=1)).mean()
    assert detail(tight) > detail(broad)
