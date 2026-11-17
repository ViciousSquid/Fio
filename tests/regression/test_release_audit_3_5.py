"""Defects found by the 3.5.0 stable-release audit, one test per invariant.

* A file command during Play must not write or clear the running game's world.
* Terrain edits made in the Terrain Editor panel are edits: the map is unsaved.
* Settings -> Apply & Restart settles unsaved work first, like closing does.
* A LogicState's values belong to a play session: a new Play starts from the
  authored ``initial_data``, Stop leaves no trace, and another map does not
  inherit them. A level change during Play still carries them.
* Load Game restores LogicState values, from a full save and from a delta.
"""
import json
import os

import pytest

pytestmark = pytest.mark.qt


def _level(initial, store="score"):
    return {
        "version": 5,
        "brushes": [{"id": "floor", "name": "floor",
                     "pos": [0, -8, 0], "size": [512, 16, 512]}],
        "things": [
            {"type": "playerstart", "pos": [0, 40, 0],
             "properties": {"type": "player_start", "name": "start",
                            "id": "ps-1", "primary": True}},
            {"type": "logicstate", "pos": [0, 40, 0],
             "properties": {"type": "logic_state", "name": "S", "id": "s-1",
                            "store_name": store, "initial_data": initial}},
        ],
    }


def _store(session):
    return next(t for t in session.state.things
                if t.properties.get("id") == "s-1")


def _increment(session, times=1):
    for _ in range(times):
        session.logic.io_manager._execute_input(
            "S", "Increment", "kills", "test", target_id="s-1")


@pytest.fixture
def clean_registry():
    from editor.things import LogicState
    LogicState._persistent_registry.clear()
    yield LogicState._persistent_registry
    LogicState._persistent_registry.clear()


# ---------------------------------------------------------------------------
# File commands during Play
# ---------------------------------------------------------------------------

def test_save_during_play_does_not_write_the_running_world(
        fio_session, tmp_path, clean_registry):
    session = fio_session(_level({"kills": 0}))
    window = session.window
    path = str(tmp_path / "authored.json")
    window.file_path = path
    window.save_level()
    authored = open(path, encoding="utf-8").read()

    session.start_play()
    session.step(2)
    _increment(session, 3)
    window.save_level()
    window.unsaved_changes = True
    window.autosave()
    session.stop_play()

    assert open(path, encoding="utf-8").read() == authored, (
        "Save during Play replaced the authored map with the play world")
    autosave = os.path.join(window.root_dir, "maps", "authored_autosave.json")
    assert not os.path.exists(autosave), "autosave ran during Play"
    window.unsaved_changes = False


def test_new_map_during_play_is_refused(fio_session, clean_registry):
    session = fio_session(_level({"kills": 0}))
    session.start_play()
    session.step(1)
    things = len(session.state.things)
    # Nothing unsaved, so no prompt stands between the command and the scene.
    session.window.unsaved_changes = False

    session.window.new_map()

    assert session.playing
    assert len(session.state.things) == things, (
        "New Map cleared the scene under the running session")
    session.stop_play()


def test_closing_during_play_leaves_play_before_it_prompts(
        fio_session, clean_registry, monkeypatch):
    session = fio_session(_level({"kills": 0}))
    window = session.window
    session.start_play()
    session.step(1)
    seen = []
    monkeypatch.setattr(
        window, "check_unsaved_changes",
        lambda: seen.append(bool(window.view_3d.play_mode)) or False)

    assert not window.close(), "the stubbed prompt cancels the close"
    assert seen == [False], "the unsaved-changes prompt ran while still in Play"


# ---------------------------------------------------------------------------
# Terrain edits mark the map unsaved
# ---------------------------------------------------------------------------

def test_terrain_panel_edits_mark_the_map_unsaved(main_window):
    from engine.terrain import Terrain

    window = main_window
    window.terrain = Terrain(seed=42)
    window.state.terrain_data = window.terrain.to_dict()
    window.unsaved_changes = False

    window.on_terrain_changed()

    assert window.unsaved_changes, (
        "a Terrain Editor change left the map marked as saved")


# ---------------------------------------------------------------------------
# Apply & Restart
# ---------------------------------------------------------------------------

def _settings_window(main_window, monkeypatch, answer):
    from editor import SettingsWindow as module

    dialog = module.SettingsWindow.__new__(module.SettingsWindow)
    dialog.main_window = main_window
    calls = {"prompt": 0, "restart": 0}

    def prompt():
        calls["prompt"] += 1
        return answer

    monkeypatch.setattr(main_window, "check_unsaved_changes", prompt)
    monkeypatch.setattr(dialog, "_save_settings", lambda: None, raising=False)
    monkeypatch.setattr(main_window, "save_config", lambda: None)
    monkeypatch.setattr(
        dialog, "_restart_application",
        lambda: calls.__setitem__("restart", calls["restart"] + 1),
        raising=False)
    return dialog, calls


def test_apply_and_restart_is_cancelled_with_the_unsaved_prompt(
        main_window, monkeypatch):
    dialog, calls = _settings_window(main_window, monkeypatch, answer=False)
    main_window.unsaved_changes = True

    dialog._apply_and_restart()

    assert calls == {"prompt": 1, "restart": 0}, (
        "Apply & Restart went ahead without settling unsaved work")


def test_apply_and_restart_proceeds_once_work_is_settled(
        main_window, monkeypatch):
    dialog, calls = _settings_window(main_window, monkeypatch, answer=True)

    dialog._apply_and_restart()

    assert calls == {"prompt": 1, "restart": 1}


# ---------------------------------------------------------------------------
# LogicState values belong to the play session
# ---------------------------------------------------------------------------

def test_a_playtest_leaves_no_logic_state_behind(fio_session, clean_registry):
    session = fio_session(_level({"kills": 0}))
    session.start_play()
    session.step(2)
    _increment(session, 3)
    assert _store(session).get_value("kills") == 3
    session.stop_play()

    assert _store(session).get_value("kills") == 0, (
        "Stop left the playtest's value on the editor's store")
    record = next(t for t in session.state.get_level_data()["things"]
                  if t["properties"].get("id") == "s-1")
    assert record.get("runtime_data") == {"kills": 0}, (
        "a save after Stop would write the playtest's values into the map")

    session.start_play()
    session.step(1)
    assert _store(session).get_value("kills") == 0, (
        "the second Play started from the first one's value")
    session.stop_play()


def test_play_starts_from_the_initial_data_as_last_edited(
        fio_session, clean_registry):
    session = fio_session(_level({"kills": 0}))
    # An undo checkpoint serialises the store, which registers its values.
    session.window.save_state()
    _store(session).properties["initial_data"] = {"kills": 10}

    session.start_play()
    session.step(1)
    assert _store(session).get_value("kills") == 10, (
        "an edit to initial_data did not reach the next Play")
    session.stop_play()
    session.window.unsaved_changes = False


def test_another_map_does_not_inherit_a_store_of_the_same_name(
        fio_session, clean_registry):
    session = fio_session(_level({"kills": 0}))
    session.start_play()
    session.step(1)
    _increment(session, 3)
    session.stop_play()
    clean_registry["score"] = {"kills": 3}      # as a non-restoring Stop leaves it

    assert session.window._load_level(_level({"kills": 0, "other": 5}))

    store = _store(session)
    assert (store.get_value("kills"), store.get_value("other")) == (0, 5)


def test_a_level_change_during_play_still_carries_the_stores(
        fio_session, clean_registry):
    session = fio_session(_level({"kills": 0}))
    session.start_play()
    session.step(1)
    _increment(session, 3)

    assert session.window._load_level(_level({"kills": 0}), from_game=True)

    assert session.playing
    assert _store(session).get_value("kills") == 3, (
        "a level change during Play dropped the carried store")
    session.stop_play()


# ---------------------------------------------------------------------------
# Load Game restores LogicState values
# ---------------------------------------------------------------------------

def test_load_game_restores_logic_state_values(
        fio_session, tmp_path, clean_registry):
    session = fio_session(_level({"kills": 0}))
    session.start_play()
    session.step(2)
    _increment(session, 3)
    path = str(tmp_path / "slot.fiosave")
    assert session.logic.session_runtime.save_session(path)[0]
    _increment(session, 2)
    assert _store(session).get_value("kills") == 5

    assert session.logic.session_runtime.load_session(path)[0]

    assert _store(session).get_value("kills") == 3
    assert clean_registry["score"] == {"kills": 3}
    session.stop_play()


def test_a_delta_save_records_and_restores_logic_state_values(
        fio_session, clean_registry):
    from engine import savegame

    level = _level({"kills": 0})
    session = fio_session(level)
    session.start_play()
    session.step(2)
    base = savegame.normalize_base_level(json.loads(json.dumps(level)))
    _increment(session, 3)

    snapshot = savegame.build_snapshot(
        session.logic, map_name="m.json",
        save_mode=savegame.SAVE_MODE_DELTA, base_level=base)
    snapshot = json.loads(json.dumps(snapshot, default=savegame._json_default))
    saved = [t["properties"]["id"] for t in snapshot["delta"]["level"]["things"]]
    assert "s-1" in saved, "a changed store was left out of the delta"

    _increment(session, 2)
    savegame.restore_delta(session.logic, snapshot, base)
    assert _store(session).get_value("kills") == 3

    # A store the delta does not mention goes back to the base map's values.
    snapshot["delta"]["level"]["things"] = []
    _increment(session, 4)
    savegame.restore_delta(session.logic, snapshot, base)
    assert _store(session).get_value("kills") == 0
    session.stop_play()
