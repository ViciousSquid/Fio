"""Opening a map puts the editor camera at its PlayerStart, and it stays there.

The editor camera is owned by the logic thread: every paint copies the view's
camera back from the published render state. A camera move made only on the
view is undone by the next frame, so "Focus Player_Start on load" worked only
when the logic thread had not started yet (and the camera then faced away
from the spawn). These tests check the copy every paint reads from.
"""

import json
import math

import pytest

pytest.importorskip("PyQt5", reason="level loading lives on the editor window")

pytestmark = pytest.mark.qt


def _start(pos, angle, name="PlayerStart_1"):
    return {"type": "playerstart", "pos": list(pos),
            "properties": {"type": "playerstart", "name": name, "angle": angle}}


def _write_map(tmp_path, name, things):
    path = tmp_path / "maps" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 3, "brushes": [], "things": things}))
    return str(path)


def _editor_camera(window):
    return window.view_3d.logic_thread.camera.get_editor_camera()


def _play_view_yaw(angle):
    """The yaw the play view shows for a PlayerStart at *angle* degrees."""
    player_angle = math.radians(90.0 - angle) + math.pi
    return 90.0 - math.degrees(player_angle)


def _same_heading(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0) < 1e-6


@pytest.mark.parametrize("angle", [0.0, 45.0, 90.0, 270.0])
def test_opening_a_map_puts_the_editor_camera_at_its_player_start(main_window, tmp_path, angle):
    window = main_window
    assert window.view_3d.logic_thread is not None
    path = _write_map(tmp_path, "start.json", [_start((300.0, 32.0, -500.0), angle)])

    assert window.load_level_file(path)

    camera = _editor_camera(window)
    assert tuple(camera.pos) == (300.0, 32.0, -500.0)
    # Facing the way the player will spawn facing.
    assert _same_heading(camera.yaw, _play_view_yaw(angle))
    assert camera.pitch == 0.0
    view = window.view_3d.camera
    assert (tuple(view.pos), view.yaw, view.pitch) == (tuple(camera.pos), camera.yaw, camera.pitch)


def test_every_map_opened_moves_the_camera_not_just_the_first(main_window, tmp_path):
    window = main_window
    first = _write_map(tmp_path, "first.json", [_start((100.0, 0.0, 100.0), 0.0)])
    second = _write_map(tmp_path, "second.json", [_start((-800.0, 64.0, 1200.0), 90.0)])

    assert window.load_level_file(first)
    window.view_3d.logic_thread.camera.set_editor_camera(
        (5000.0, 5000.0, 5000.0), 10.0, -30.0, window.view_3d.camera.fov)  # flown away
    assert window.load_level_file(second)

    assert tuple(_editor_camera(window).pos) == (-800.0, 64.0, 1200.0)


def test_the_primary_start_wins_when_a_map_has_several(main_window, tmp_path):
    window = main_window
    things = [_start((10.0, 0.0, 10.0), 0.0, "A"), _start((900.0, 0.0, 900.0), 0.0, "B")]
    things[1]["properties"]["primary"] = True
    assert window.load_level_file(_write_map(tmp_path, "two.json", things))
    assert tuple(_editor_camera(window).pos) == (900.0, 0.0, 900.0)


def test_with_the_setting_off_the_camera_still_goes_near_the_start(main_window, tmp_path):
    window = main_window
    if not window.config.has_section('Display'):
        window.config.add_section('Display')
    window.config.set('Display', 'place_camera_at_player_start', 'False')
    assert window.load_level_file(
        _write_map(tmp_path, "off.json", [_start((300.0, 32.0, -500.0), 45.0)]))
    camera = _editor_camera(window)
    assert tuple(camera.pos) == (300.0, 112.0, -300.0)
    assert (camera.yaw, camera.pitch) == (-90.0, -20.0)


def test_a_map_without_a_player_start_resets_the_camera(main_window, tmp_path):
    window = main_window
    window.view_3d.logic_thread.camera.set_editor_camera(
        (5000.0, 5000.0, 5000.0), 10.0, -30.0, window.view_3d.camera.fov)
    assert window.load_level_file(_write_map(tmp_path, "empty.json", []))
    camera = _editor_camera(window)
    assert tuple(camera.pos) == (0.0, 150.0, 400.0)
    assert (camera.yaw, camera.pitch) == (-90.0, -20.0)


def test_focusing_an_object_moves_the_camera_the_logic_thread_draws_from(main_window):
    window = main_window
    window.focus_on_bounds((1000.0, 50.0, -2000.0), 64.0)
    camera = _editor_camera(window)
    front = camera.get_front_vector()
    distance = 192.0
    expected = (1000.0 - front.x * distance, 50.0 - front.y * distance,
                -2000.0 - front.z * distance)
    assert tuple(camera.pos) == pytest.approx(expected, abs=1e-3)
