"""Focused LogicCamera coverage through the production LogicThread owner."""

import glm
import pytest

pytest.importorskip("PyQt5", reason="LogicThread owns the live player runtime")

from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState
from editor.editor_state import EditorState

pytestmark = pytest.mark.qt


@pytest.fixture
def logic(request):
    state = EditorState()
    runtime = LogicThread(ThreadedGameState(), state)
    request.addfinalizer(runtime.stop)
    return runtime


@pytest.fixture
def camera(logic):
    return logic.camera


def test_logic_camera_normalizes_overhead_mode_names(camera):
    for mode in ("Overhead", "overhead", "top-down", "topdown"):
        camera.set_camera_mode(mode)
        assert camera.is_overhead() is True

    camera.set_camera_mode("First Person")
    assert camera.is_overhead() is False


def test_logic_camera_effective_height_honours_positive_ceiling(camera):
    camera.overhead_height = 800.0
    camera.overhead_height_limit = 500.0
    assert camera.effective_overhead_height() == pytest.approx(500.0)

    camera.overhead_height_limit = None
    assert camera.effective_overhead_height() == pytest.approx(800.0)


def test_logic_camera_north_overhead_looks_down_towards_negative_z(
    logic, camera
):
    player = logic.player_runtime.player
    player.pos = glm.vec3(10, 20, 30)
    camera.overhead_height = 100.0
    camera.overhead_tilt = 45.0

    cam, direction, up = camera._overhead_camera(player.pos, 0.0)

    assert direction.y < 0.0
    assert direction.z < 0.0
    assert cam.y > 20.0
    assert glm.length(direction) == pytest.approx(1.0)
    assert glm.length(up) == pytest.approx(1.0)


def test_logic_camera_player_orientation_changes_overhead_horizontal_direction(
    logic, camera
):
    player = logic.player_runtime.player
    camera.overhead_orientation = "player"
    camera.overhead_tilt = 45.0

    _, direction_a, _ = camera._overhead_camera(player.pos, 0.0)
    _, direction_b, _ = camera._overhead_camera(player.pos, 1.57079632679)

    assert direction_a.z > 0.0
    assert direction_b.x > 0.0
    assert direction_a.x != pytest.approx(direction_b.x)


def test_logic_camera_footprint_is_disabled_outside_overhead_mode(camera):
    assert camera.overhead_ground_footprint() is None


def test_logic_camera_transition_completes_and_clears_state(camera):
    assert camera.start_camera_transition("overhead", duration=0.5) == "Overhead"
    assert camera.camera_transition is not None

    camera.update_camera_transition(0.25)
    assert camera.camera_transition is not None
    camera.update_camera_transition(0.25)

    assert camera.camera_transition is None
    assert camera.is_overhead() is True


def test_logic_camera_zero_duration_transition_is_immediate(camera):
    assert camera.start_camera_transition("overhead", duration=0.0) == "Overhead"
    assert camera.camera_transition is None
    assert camera.is_overhead() is True
