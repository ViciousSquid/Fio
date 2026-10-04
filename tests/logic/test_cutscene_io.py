"""Cutscene I/O coverage through the production LogicThread and CutsceneRuntime."""

import glm
import pytest

pytest.importorskip("PyQt5", reason="LogicThread owns editor-backed entities")

from editor.editor_state import EditorState
from editor.things import Thing
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState
from tests.helpers.worlds import make_thing

pytestmark = pytest.mark.qt


@pytest.fixture
def logic(request):
    state = EditorState()
    runtime = LogicThread(ThreadedGameState(), state)
    request.addfinalizer(runtime.stop)
    return runtime


def _prepare_cutscene(logic):
    source = make_thing(
        Thing,
        "Door",
        (0.0, 0.0, 0.0),
        id="source-1",
        type="thing",
    )
    logic.editor_state.things[:] = [source]
    logic.editor_state.brushes[:] = []
    logic.world_runtime.build_entity_caches()
    logic.cutscene_runtime.state = {
        "active": True,
        "elapsed": 0.0,
        "io_events": [
            {
                "time": 0.0,
                "source_id": "source-1",
                "source_name": "Door",
                "output": "Open",
            },
            {
                "time": 1.0,
                "source_id": "source-1",
                "source_name": "Door",
                "output": "SetValue",
                "parameter": "42",
            },
        ],
        "next_io_event": 0,
    }
    return source


def test_cutscene_io_events_fire_at_authored_times_and_only_once(logic, monkeypatch):
    source = _prepare_cutscene(logic)
    calls = []
    real_fire_output = logic.io_manager.fire_output

    def record(entity, output, value=None):
        calls.append((entity, output, value))
        return real_fire_output(entity, output, value)

    monkeypatch.setattr(logic.io_manager, "fire_output", record)

    assert logic.cutscene_runtime._fire_cinematic_io_events() is True
    assert [(output, value) for _, output, value in calls] == [
        ("Open", None)
    ]

    logic.cutscene_runtime.state["elapsed"] = 1.0
    assert logic.cutscene_runtime._fire_cinematic_io_events() is True
    assert [(output, value) for _, output, value in calls] == [
        ("Open", None),
        ("SetValue", "42"),
    ]

    assert logic.cutscene_runtime._fire_cinematic_io_events() is True
    assert len(calls) == 2
    assert all(entity is source for entity, _, _ in calls)


def test_cutscene_io_output_can_stop_the_active_camera(logic, monkeypatch):
    source = _prepare_cutscene(logic)
    logic.cutscene_runtime.state["io_events"] = [{
        "time": 0.0,
        "source_id": "source-1",
        "source_name": "Door",
        "output": "StopCutscene",
    }]

    real_fire_output = logic.io_manager.fire_output

    def fire_and_stop(entity, output, value=None):
        result = real_fire_output(entity, output, value)
        logic.cutscene_runtime.state = None
        return result

    monkeypatch.setattr(logic.io_manager, "fire_output", fire_and_stop)

    assert logic.cutscene_runtime._fire_cinematic_io_events() is False
    assert logic.cutscene_runtime.state is None


def test_cutscene_camera_teleport_holds_previous_shot_until_destination_time():
    from engine.cutscene_runtime import CutsceneRuntime

    rows = [
        {"time": 0.0, "pos": [0, 0, 0], "yaw": 0.0, "pitch": 0.0, "fov": 90.0},
        {
            "time": 3.0,
            "pos": [1200, 80, -900],
            "yaw": 180.0,
            "pitch": 15.0,
            "fov": 70.0,
            "teleport": True,
        },
    ]

    before_cut = CutsceneRuntime._cutscene_sample(rows, 2.5)
    assert before_cut["pos"] == [0.0, 0.0, 0.0]
    assert before_cut["yaw"] == 0.0
    assert before_cut["pitch"] == 0.0
    assert before_cut["fov"] == 90.0

    at_cut = CutsceneRuntime._cutscene_sample(rows, 3.0)
    assert at_cut["pos"] == [1200.0, 80.0, -900.0]
    assert at_cut["yaw"] == 180.0
    assert at_cut["pitch"] == 15.0
    assert at_cut["fov"] == 70.0


def test_cutscene_camera_keyframes_still_interpolate_without_teleport():
    from engine.cutscene_runtime import CutsceneRuntime

    rows = [
        {"time": 0.0, "pos": [0, 0, 0], "yaw": 0.0},
        {"time": 2.0, "pos": [100, 20, 40], "yaw": 1.0},
    ]

    sample = CutsceneRuntime._cutscene_sample(rows, 1.0)
    assert sample["pos"] == [50.0, 10.0, 20.0]
    assert sample["yaw"] == 0.5
