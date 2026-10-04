"""Cutscene preview and actor cleanup through the real CutsceneWizard/MainWindow."""

from pathlib import Path

import glm
import pytest

pytest.importorskip("PyQt5", reason="the cutscene wizard uses Qt")

from editor.cutscene_wizard import CutsceneWizard
from editor.editor_state import EditorState
from editor.things import LogicCamera, Monster
from tests.helpers.worlds import make_thing

pytestmark = pytest.mark.qt


@pytest.fixture
def cutscene_wizard(main_window, qt_app):
    wizard = CutsceneWizard(main_window)
    wizard.show()
    qt_app.processEvents()
    try:
        yield wizard
    finally:
        wizard.close()
        wizard.deleteLater()
        qt_app.processEvents()


def _actor(name, actor_id, pos):
    return make_thing(
        Monster,
        name,
        pos,
        id=actor_id,
        display_name=name,
        monster_type="human",
        triggered=True,
        _cutscene_temporary=True,
    )


def _camera_baseline(wizard):
    camera = wizard.main_window.view_3d.camera
    return {
        "pos": [float(camera.pos.x), float(camera.pos.y), float(camera.pos.z)],
        "yaw": float(camera.yaw),
        "pitch": float(camera.pitch),
        "fov": float(getattr(camera, "fov", 90.0)),
    }


def test_preview_end_restores_real_actor_and_camera_and_consumes_baseline(
    cutscene_wizard, main_window
):
    wizard = cutscene_wizard
    actor = _actor("Actor", "actor", (10.0, 20.0, 30.0))
    main_window.state.things.append(actor)

    wizard.actor_objects = {"actor": actor}
    wizard.actor_meta = {
        "actor": {"id": "actor", "name": "Actor", "spawn": True}
    }
    wizard.actor_tracks = {
        "actor": [
            {"time": 0.0, "pos": [10.0, 20.0, 30.0], "yaw": 5.0},
            {"time": 1.0, "pos": [100.0, 200.0, 300.0], "yaw": 25.0},
        ],
    }
    camera = main_window.view_3d.camera
    initial_camera = _camera_baseline(wizard)
    wizard.camera_keys = [
        initial_camera | {"time": 0.0},
        {
            "time": 1.0,
            "pos": [400.0, 500.0, 600.0],
            "yaw": 50.0,
            "pitch": 60.0,
            "fov": 70.0,
        },
    ]

    wizard._preview_start()
    actor.pos = glm.vec3(900.0, 901.0, 902.0)
    actor.angle = 93.0
    camera.pos = glm.vec3(903.0, 904.0, 905.0)
    camera.yaw = 94.0
    camera.pitch = 95.0
    camera.fov = 96.0

    wizard._preview_time = wizard._preview_duration
    wizard._preview_rate = 1.0
    wizard._preview_tick()

    assert list(actor.pos) == pytest.approx([10.0, 20.0, 30.0])
    assert actor.angle == pytest.approx(5.0)
    assert list(camera.pos) == pytest.approx(initial_camera["pos"])
    assert camera.yaw == pytest.approx(initial_camera["yaw"])
    assert camera.pitch == pytest.approx(initial_camera["pitch"])
    assert camera.fov == pytest.approx(initial_camera["fov"])
    assert wizard._preview_actor_baseline == {}
    assert wizard._preview_camera_baseline is None
    assert wizard._preview_rate == 0.0
    assert wizard._preview_time == 0.0
    assert wizard._preview_duration == 0.0


def test_preview_play_pause_uses_the_real_qtimer_and_restores_state(
    cutscene_wizard, main_window
):
    wizard = cutscene_wizard
    actor = _actor("Actor", "actor", (10.0, 20.0, 30.0))
    main_window.state.things.append(actor)
    wizard.actor_objects = {"actor": actor}
    wizard.actor_meta = {
        "actor": {"id": "actor", "name": "Actor", "spawn": True}
    }
    wizard.actor_tracks = {
        "actor": [
            {"time": 0.0, "pos": [10.0, 20.0, 30.0], "yaw": 5.0},
            {"time": 2.0, "pos": [100.0, 200.0, 300.0], "yaw": 25.0},
        ],
    }
    wizard.camera_keys = []

    wizard._preview_play()

    assert wizard._preview_rate == 1.0
    assert wizard._preview_timer.isActive()

    wizard._preview_play()

    assert wizard._preview_rate == 0.0
    assert not wizard._preview_timer.isActive()
    assert list(actor.pos) == pytest.approx([10.0, 20.0, 30.0])
    assert actor.angle == pytest.approx(5.0)
    assert wizard._preview_actor_baseline == {}


def test_removing_actor_cleans_all_cutscene_references_and_real_map_state(
    cutscene_wizard, main_window
):
    wizard = cutscene_wizard
    gone = _actor("Gone", "gone", (0.0, 0.0, 0.0))
    survivor = _actor("Survivor", "survivor", (10.0, 0.0, 0.0))
    main_window.state.things[:] = [gone, survivor]

    wizard.actor_meta = {
        "gone": {"id": "gone", "name": "Gone", "spawn": True},
        "survivor": {"id": "survivor", "name": "Survivor", "spawn": True},
    }
    wizard.actor_objects = {"gone": gone, "survivor": survivor}
    wizard.temporary_actor_ids = {"gone", "survivor"}
    wizard.actor_tracks = {
        "survivor": [
            {"time": 1.0, "pos": [1, 2, 3], "target_id": "gone"}
        ],
        "gone": [{"time": 0.0, "pos": [0, 0, 0]}],
    }
    wizard.camera_keys = [
        {
            "time": 0.0,
            "pos": [0, 0, 0],
            "look_at": {"actor": "gone"},
        },
        {"time": 1.0, "pos": [1, 1, 1]},
    ]
    wizard.events = [
        {
            "time": 0,
            "type": "fight",
            "attackers": ["gone", "survivor"],
            "defenders": ["survivor"],
        },
        {
            "time": 1,
            "type": "fight",
            "attackers": ["survivor"],
            "defenders": ["gone"],
        },
        {
            "time": 2,
            "type": "dialogue",
            "speaker_id": "gone",
            "text": "Hello",
        },
        {
            "time": 3,
            "type": "message",
            "line": "message",
            "text": "Still here",
        },
    ]
    wizard._refresh_actor_lists()
    wizard._select_actor_id("gone")

    wizard._remove_selected_actors()

    assert gone not in main_window.state.things
    assert survivor in main_window.state.things
    assert "gone" not in wizard.actor_meta
    assert "gone" not in wizard.actor_objects
    assert "gone" not in wizard.actor_tracks
    assert wizard.actor_tracks["survivor"][0].get("target_id") is None
    assert all(
        row.get("look_at", {}).get("actor") != "gone"
        for row in wizard.camera_keys
    )
    assert len(wizard.events) == 3
    fight = next(e for e in wizard.events if e["type"] == "fight")
    assert fight["attackers"] == ["survivor"]
    dialogue = next(e for e in wizard.events if e["type"] == "dialogue")
    assert dialogue["speaker_id"] == ""
    message = next(e for e in wizard.events if e["type"] == "message")
    assert message["text"] == "Still here"


def test_cutscene_open_discovers_the_current_maps_real_logic_camera(
    cutscene_wizard, main_window, tmp_path
):
    wizard = cutscene_wizard
    cutscene_dir = tmp_path / "cutscenes"
    cutscene_dir.mkdir()
    expected = cutscene_dir / "corridor.json"
    expected.write_text("{}", encoding="utf-8")

    main_window.root_dir = str(tmp_path)
    camera = LogicCamera(
        pos=[0.0, 0.0, 0.0],
        properties={
            "name": "Camera",
            "cutscene_file": "cutscenes/corridor.json",
        },
    )
    main_window.state.things[:] = [camera]

    paths = wizard._current_map_cutscene_files()

    assert paths == [Path(expected).resolve()]


def test_cutscene_map_discovery_deduplicates_real_logic_camera_references(
    cutscene_wizard, main_window, tmp_path
):
    wizard = cutscene_wizard
    cutscene_dir = tmp_path / "cutscenes"
    cutscene_dir.mkdir()
    expected = cutscene_dir / "corridor.json"
    expected.write_text("{}", encoding="utf-8")

    main_window.root_dir = str(tmp_path)
    main_window.state.things[:] = [
        LogicCamera(
            pos=[0.0, 0.0, 0.0],
            properties={
                "name": "Camera A",
                "cutscene_file": "cutscenes/corridor.json",
            },
        ),
        LogicCamera(
            pos=[100.0, 0.0, 0.0],
            properties={
                "name": "Camera B",
                "cutscene_file": "cutscenes/corridor.json",
            },
        ),
    ]

    paths = wizard._current_map_cutscene_files()

    assert paths == [Path(expected).resolve()]
