"""Settings > Editor > 3D View > "Hidden lights give no light".

With it on, a light hidden with H stops lighting the editor's 3D view until it
is shown again (Shift+H); off (the default), a hidden light lights the editor
as it always did. Measured from the painted frame.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

import glm                                                   # noqa: E402

from editor.things import Light, PlayerStart                 # noqa: E402
from tests.helpers.session import process_events             # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 150.0, -300.0), radius=900.0,
                      intensity=3.0, casts_shadows=False)
    return level_data(brushes=room(size=1024.0, height=384.0), things=[spawn, lamp])


def _brightness(session):
    process_events(session.app)
    session.publish()
    session.paint()
    session.publish()
    return float(session.paint().astype(np.float64).mean())


def _hide(session, lamp):
    session.window.set_selected_objects([lamp])
    session.window.hide_selected_brush()


@pytest.mark.parametrize("setting", [False, True])
def test_a_hidden_light_lights_the_editor_only_with_the_setting_off(fio_session, setting):
    session = fio_session(_level(), size=(200, 150))
    session.view.sysmon.set_active(False)
    window, view = session.window, session.view
    camera = view.logic_thread.camera.get_editor_camera()
    camera.pos = glm.vec3(0.0, 150.0, 300.0)
    camera.yaw, camera.pitch = -90.0, -10.0          # towards the lamp's wall
    window.config.set('Display', 'hide_hidden_lights', str(setting))
    view.hide_hidden_lights = setting
    lamp = next(t for t in session.state.things if isinstance(t, Light))

    lit = _brightness(session)
    _hide(session, lamp)
    assert lamp.properties.get('hidden')
    hidden = _brightness(session)
    window.unhide_all_brushes()
    shown = _brightness(session)

    assert shown == pytest.approx(lit, abs=0.5)
    if setting:
        assert hidden < lit - 5.0, (lit, hidden)
    else:
        assert hidden == pytest.approx(lit, abs=0.5)


def test_the_setting_is_in_the_settings_window(qt_app):
    import configparser
    from editor.SettingsWindow import SettingsWindow
    config = configparser.ConfigParser()
    config.add_section('Display')
    config.set('Display', 'hide_hidden_lights', 'True')
    dialog = SettingsWindow(config, None)
    try:
        box = dialog.hide_hidden_lights_checkbox
        assert box.isChecked()
        box.setChecked(False)
        dialog.accept()
        assert config.getboolean('Display', 'hide_hidden_lights') is False
    finally:
        dialog.deleteLater()
