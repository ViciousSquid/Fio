"""Focused unit tests for the extracted LogicCamera runtime."""

from types import SimpleNamespace

import glm
import pytest

from engine.logic_camera import LogicCamera


def _runtime(pos=(0, 0, 0), angle=0.0):
    player = SimpleNamespace(pos=glm.vec3(*pos), angle=angle, pitch=0.0)
    return LogicCamera(SimpleNamespace(player=player))


def test_logic_camera_normalizes_overhead_mode_names():
    runtime = _runtime()
    for mode in ("Overhead", "overhead", "top-down", "topdown"):
        runtime.set_camera_mode(mode)
        assert runtime.is_overhead() is True

    runtime.set_camera_mode("First Person")
    assert runtime.is_overhead() is False


def test_logic_camera_effective_height_honours_positive_ceiling():
    runtime = _runtime()
    runtime.overhead_height = 800.0
    runtime.overhead_height_limit = 500.0
    assert runtime.effective_overhead_height() == pytest.approx(500.0)

    runtime.overhead_height_limit = None
    assert runtime.effective_overhead_height() == pytest.approx(800.0)


def test_logic_camera_north_overhead_looks_down_towards_negative_z():
    runtime = _runtime(pos=(10, 20, 30))
    runtime.overhead_height = 100.0
    runtime.overhead_tilt = 45.0
    cam, direction, up = runtime._overhead_camera(runtime._host.player.pos, 0.0)

    assert direction.y < 0.0
    assert direction.z < 0.0
    assert cam.y > 20.0
    assert glm.length(direction) == pytest.approx(1.0)
    assert glm.length(up) == pytest.approx(1.0)


def test_logic_camera_player_orientation_changes_overhead_horizontal_direction():
    runtime = _runtime()
    runtime.overhead_orientation = "player"
    runtime.overhead_tilt = 45.0

    _, direction_a, _ = runtime._overhead_camera(runtime._host.player.pos, 0.0)
    _, direction_b, _ = runtime._overhead_camera(runtime._host.player.pos, 1.57079632679)

    # Player angle 0 faces +Z in the engine's first-person convention;
    # the overhead camera preserves that horizontal heading while raking down.
    assert direction_a.z > 0.0
    assert direction_b.x > 0.0
    assert direction_a.x != pytest.approx(direction_b.x)


def test_logic_camera_footprint_is_disabled_outside_overhead_mode():
    runtime = _runtime()
    assert runtime.overhead_ground_footprint() is None


def test_logic_camera_transition_completes_and_clears_state():
    runtime = _runtime()
    assert runtime.start_camera_transition("overhead", duration=0.5) == "Overhead"
    assert runtime.camera_transition is not None

    runtime.update_camera_transition(0.25)
    assert runtime.camera_transition is not None
    runtime.update_camera_transition(0.25)

    assert runtime.camera_transition is None
    assert runtime.is_overhead() is True


def test_logic_camera_zero_duration_transition_is_immediate():
    runtime = _runtime()
    assert runtime.start_camera_transition("overhead", duration=0.0) == "Overhead"
    assert runtime.camera_transition is None
    assert runtime.is_overhead() is True
