import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")
pytestmark = pytest.mark.qt

from types import SimpleNamespace

from engine.cutscene_runtime import CutsceneRuntime
from engine.logic_world import LogicWorld
from editor.things import ENTITY_TYPES


class RecordingIO:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def fire_output(self, entity, output, value=None):
        self.calls.append((entity, output, value))
        if output == "StopCutscene":
            self.owner.runtime.state = None


class CutsceneWorld:
    def __init__(self):
        self.source = SimpleNamespace(properties={"id": "source-1", "name": "Door"})
        self.editor_state = SimpleNamespace(things=[self.source], brushes=[])
        self.io_manager = RecordingIO(self)
        self.session_runtime = SimpleNamespace(play_mode=False)
        self.runtime = CutsceneRuntime(self)
        self.world_runtime = LogicWorld(self)

        self.runtime.state = {
            "active": True,
            "elapsed": 0.0,
            "io_events": [
                {"time": 0.0, "source_id": "source-1", "source_name": "Door", "output": "Open"},
                {"time": 1.0, "source_id": "source-1", "source_name": "Door", "output": "SetValue", "parameter": "42"},
            ],
            "next_io_event": 0,
        }


def test_cutscene_io_events_fire_at_authored_times_and_only_once():
    world = CutsceneWorld()

    assert world.runtime._fire_cinematic_io_events() is True
    assert [(name, value) for _, name, value in world.io_manager.calls] == [("Open", None)]

    world.runtime.state["elapsed"] = 1.0
    assert world.runtime._fire_cinematic_io_events() is True
    assert [(name, value) for _, name, value in world.io_manager.calls] == [
        ("Open", None),
        ("SetValue", "42"),
    ]

    assert world.runtime._fire_cinematic_io_events() is True
    assert len(world.io_manager.calls) == 2


def test_cutscene_io_output_can_stop_the_active_camera():
    world = CutsceneWorld()
    world.runtime.state["io_events"] = [{
        "time": 0.0,
        "source_id": "source-1",
        "source_name": "Door",
        "output": "StopCutscene",
    }]

    assert world.runtime._fire_cinematic_io_events() is False
    assert world.runtime.state is None


def test_cutscene_camera_teleport_holds_previous_shot_until_destination_time():
    rows = [
        {"time": 0.0, "pos": [0, 0, 0], "yaw": 0.0, "pitch": 0.0, "fov": 90.0},
        {"time": 3.0, "pos": [1200, 80, -900], "yaw": 180.0, "pitch": 15.0, "fov": 70.0, "teleport": True},
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
    rows = [
        {"time": 0.0, "pos": [0, 0, 0], "yaw": 0.0},
        {"time": 2.0, "pos": [100, 20, 40], "yaw": 1.0},
    ]

    sample = CutsceneRuntime._cutscene_sample(rows, 1.0)
    assert sample["pos"] == [50.0, 10.0, 20.0]
    assert sample["yaw"] == 0.5
