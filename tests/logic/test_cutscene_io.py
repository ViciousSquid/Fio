from types import SimpleNamespace

from engine.logic_thread import LogicThread


class RecordingIO:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def fire_output(self, entity, output, value=None):
        self.calls.append((entity, output, value))
        if output == "StopCutscene":
            self.owner.cinematic_state = None


class CutsceneWorld:
    def __init__(self):
        self.source = SimpleNamespace(properties={"id": "source-1", "name": "Door"})
        self.io_manager = RecordingIO(self)
        self.cinematic_state = {
            "active": True,
            "elapsed": 0.0,
            "io_events": [
                {"time": 0.0, "source_id": "source-1", "source_name": "Door", "output": "Open"},
                {"time": 1.0, "source_id": "source-1", "source_name": "Door", "output": "SetValue", "parameter": "42"},
            ],
            "next_io_event": 0,
        }

    def _find_entity_by_id(self, entity_id):
        return self.source if entity_id == "source-1" else None

    def _find_entity_by_name(self, name):
        return self.source if name == "Door" else None


def test_cutscene_io_events_fire_at_authored_times_and_only_once():
    world = CutsceneWorld()

    assert LogicThread._fire_cinematic_io_events(world) is True
    assert [(name, value) for _, name, value in world.io_manager.calls] == [("Open", None)]

    world.cinematic_state["elapsed"] = 1.0
    assert LogicThread._fire_cinematic_io_events(world) is True
    assert [(name, value) for _, name, value in world.io_manager.calls] == [
        ("Open", None),
        ("SetValue", "42"),
    ]

    assert LogicThread._fire_cinematic_io_events(world) is True
    assert len(world.io_manager.calls) == 2


def test_cutscene_io_output_can_stop_the_active_camera():
    world = CutsceneWorld()
    world.cinematic_state["io_events"] = [{
        "time": 0.0,
        "source_id": "source-1",
        "source_name": "Door",
        "output": "StopCutscene",
    }]

    assert LogicThread._fire_cinematic_io_events(world) is False
    assert world.cinematic_state is None
