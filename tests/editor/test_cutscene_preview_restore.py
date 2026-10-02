"""Regression tests for modeless cutscene preview restoration."""

import types

import pytest

pytest.importorskip("PyQt5", reason="the cutscene wizard uses Qt")

from editor.cutscene_wizard import CutsceneWizard  # noqa: E402


class _Timer:
    def __init__(self):
        self.stop_calls = 0
        self.start_calls = 0

    def stop(self):
        self.stop_calls += 1

    def start(self):
        self.start_calls += 1


class _Button:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _Vec3:
    def __init__(self, x, y, z):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))


class _Actor:
    def __init__(self, pos, angle):
        self.pos = _Vec3(*pos)
        self.angle = float(angle)


class _Camera:
    def __init__(self):
        self.pos = _Vec3(30, 40, 50)
        self.yaw = 31.0
        self.pitch = 32.0
        self.fov = 71.0


def _fake_wizard():
    actor = _Actor((10, 20, 30), 45.0)
    camera = _Camera()
    return types.SimpleNamespace(
        _preview_rate=1.0,
        _preview_time=0.98,
        _preview_duration=1.0,
        _preview_timer=_Timer(),
        play_button=_Button(),
        preview_time_label=_Button(),
        _preview_actor_baseline={
            "actor": {"pos": [1.0, 2.0, 3.0], "yaw": 4.0},
        },
        _preview_camera_baseline={
            "pos": [4.0, 5.0, 6.0],
            "yaw": 7.0,
            "pitch": 8.0,
            "fov": 9.0,
        },
        actor_objects={"actor": actor},
        main_window=types.SimpleNamespace(
            view_3d=types.SimpleNamespace(
                camera=camera,
                logic_thread=None,
            ),
            update_all_ui=lambda: None,
        ),
    )


def _pos(obj):
    return list(obj.pos)


def test_preview_end_restores_actor_and_camera_and_consumes_baseline():
    wizard = _fake_wizard()

    CutsceneWizard._preview_tick(wizard)

    assert _pos(wizard.actor_objects["actor"]) == [1.0, 2.0, 3.0]
    assert wizard.actor_objects["actor"].angle == 4.0
    assert _pos(wizard.main_window.view_3d.camera) == [4.0, 5.0, 6.0]
    assert wizard.main_window.view_3d.camera.yaw == 7.0
    assert wizard.main_window.view_3d.camera.pitch == 8.0
    assert wizard.main_window.view_3d.camera.fov == 9.0

    assert wizard._preview_actor_baseline == {}
    assert wizard._preview_camera_baseline is None
    assert wizard._preview_rate == 0.0
    assert wizard._preview_time == 0.0
    assert wizard._preview_duration == 0.0
    assert wizard._preview_timer.stop_calls == 1


def test_preview_end_does_not_restore_a_later_editor_move():
    wizard = _fake_wizard()

    CutsceneWizard._preview_tick(wizard)

    actor = wizard.actor_objects["actor"]
    actor.pos = _Vec3(90, 91, 92)
    actor.angle = 93.0

    # This is what accept() does before serialising the cutscene. Once the
    # playback baseline has been consumed, it must be a no-op.
    CutsceneWizard._preview_stop_and_restore(wizard)

    assert _pos(actor) == [90.0, 91.0, 92.0]
    assert actor.angle == 93.0


def test_preview_pause_restores_and_clears_baseline():
    wizard = _fake_wizard()
    wizard._preview_start = lambda: None

    CutsceneWizard._preview_play(wizard)

    assert _pos(wizard.actor_objects["actor"]) == [1.0, 2.0, 3.0]
    assert _pos(wizard.main_window.view_3d.camera) == [4.0, 5.0, 6.0]
    assert wizard._preview_actor_baseline == {}
    assert wizard._preview_camera_baseline is None
    assert wizard._preview_rate == 0.0
    assert wizard._preview_timer.stop_calls == 1
