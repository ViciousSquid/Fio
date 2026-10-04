"""Regression tests for modeless cutscene preview restoration."""

import types

import pytest

pytest.importorskip("PyQt5", reason="the cutscene wizard uses Qt")

from editor.cutscene_wizard import CutsceneWizard  # noqa: E402
from editor.editor_state import EditorState  # noqa: E402


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
    state = EditorState()
    state.things = [actor]
    wizard = types.SimpleNamespace(
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
        actor_meta={"actor": {}},
        actor_objects={"actor": actor},
        main_window=types.SimpleNamespace(
            state=state,
            view_3d=types.SimpleNamespace(
                camera=camera,
                logic_thread=types.SimpleNamespace(
                    camera=types.SimpleNamespace(
                        set_editor_camera=lambda pos, yaw, pitch, fov: None,
                    ),
                ),
            ),
            update_all_ui=lambda: None,
        ),
    )
    wizard._find_map_actor = types.MethodType(
        CutsceneWizard._find_map_actor, wizard
    )
    wizard._refresh_actor_objects_from_state = types.MethodType(
        CutsceneWizard._refresh_actor_objects_from_state, wizard
    )
    wizard._preview_stop_and_restore = types.MethodType(
        CutsceneWizard._preview_stop_and_restore, wizard
    )
    return wizard


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


class _CurrentItem:
    def __init__(self, aid):
        self._aid = aid

    def data(self, _role):
        return self._aid


class _ActorList:
    def __init__(self, aid):
        self._item = _CurrentItem(aid)

    def currentItem(self):
        return self._item


def test_removing_actor_cleans_all_cutscene_references():
    """Deleting an actor cannot leave IDs pointing at a nonexistent actor."""
    wizard = types.SimpleNamespace(
        actor_list=_ActorList("gone"),
        actor_meta={
            "gone": {"id": "gone", "name": "Gone"},
            "survivor": {"id": "survivor", "name": "Survivor"},
        },
        actor_objects={"gone": object(), "survivor": object()},
        temporary_actor_ids=set(),
        actor_tracks={
            "survivor": [{"time": 1.0, "pos": [1, 2, 3], "target_id": "gone"}],
            "gone": [{"time": 0.0, "pos": [0, 0, 0]}],
        },
        camera_keys=[
            {"time": 0.0, "pos": [0, 0, 0], "look_at": {"actor": "gone"}},
            {"time": 1.0, "pos": [1, 1, 1]},
        ],
        events=[
            {"time": 0, "type": "fight", "attackers": ["gone", "survivor"], "defenders": ["survivor"]},
            {"time": 1, "type": "fight", "attackers": ["survivor"], "defenders": ["gone"]},
            {"time": 2, "type": "dialogue", "speaker_id": "gone", "text": "Hello"},
            {"time": 3, "type": "message", "line": "message", "text": "Still here"},
        ],
        _refresh_actor_lists=lambda: None,
        _refresh_waypoints=lambda: None,
        _refresh_event_list=lambda: None,
        _refresh_camera_list=lambda: None,
        main_window=types.SimpleNamespace(
            state=types.SimpleNamespace(things=[]),
            update_all_ui=lambda: None,
        ),
        _refresh_summary=lambda: None,
    )

    CutsceneWizard._remove_selected_actors(wizard)

    assert "gone" not in wizard.actor_meta
    assert "gone" not in wizard.actor_objects
    assert "gone" not in wizard.actor_tracks
    assert wizard.actor_tracks["survivor"][0].get("target_id") is None
    assert all(row.get("look_at", {}).get("actor") != "gone" for row in wizard.camera_keys)
    assert len(wizard.events) == 3
    fight = next(e for e in wizard.events if e["type"] == "fight")
    assert fight["attackers"] == ["survivor"]
    dialogue = next(e for e in wizard.events if e["type"] == "dialogue")
    assert dialogue["speaker_id"] == ""
    message = next(e for e in wizard.events if e["type"] == "message")
    assert message["text"] == "Still here"


class LogicCamera:
    def __init__(self, cutscene_file):
        self.properties = {
            "type": "logic_camera",
            "cutscene_file": cutscene_file,
        }


def test_cutscene_open_discovers_the_current_maps_logic_camera(tmp_path):
    """Opening the wizard must follow the loaded map, not alphabetic cutscene order."""
    cutscene_dir = tmp_path / "cutscenes"
    cutscene_dir.mkdir()
    expected = cutscene_dir / "corridor.json"
    expected.write_text("{}", encoding="utf-8")
    distractor = cutscene_dir / "showcase.json"
    distractor.write_text("{}", encoding="utf-8")

    wizard = types.SimpleNamespace(
        main_window=types.SimpleNamespace(
            root_dir=str(tmp_path),
            state=types.SimpleNamespace(
                things=[
                    LogicCamera("cutscenes/corridor.json"),
                ],
            ),
        ),
    )
    loaded = []
    wizard._load_cutscene = loaded.append
    wizard._current_map_cutscene_files = CutsceneWizard._current_map_cutscene_files.__get__(
        wizard, type(wizard)
    )

    CutsceneWizard._load_current_map_cutscene(wizard)

    assert loaded == [str(expected)]


def test_cutscene_map_discovery_deduplicates_logic_camera_references(tmp_path):
    cutscene_dir = tmp_path / "cutscenes"
    cutscene_dir.mkdir()
    expected = cutscene_dir / "corridor.json"
    expected.write_text("{}", encoding="utf-8")

    wizard = types.SimpleNamespace(
        main_window=types.SimpleNamespace(
            root_dir=str(tmp_path),
            state=types.SimpleNamespace(
                things=[
                    LogicCamera("cutscenes/corridor.json"),
                    LogicCamera("cutscenes/corridor.json"),
                ],
            ),
        ),
    )

    paths = CutsceneWizard._current_map_cutscene_files(wizard)

    assert paths == [expected]
