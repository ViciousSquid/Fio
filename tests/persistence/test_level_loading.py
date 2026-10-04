"""Which file the open level belongs to, after every way a level can arrive.

``save_level`` writes ``file_path`` with no questions asked, so ``file_path`` is
a promise: "this scene is that file".  Two load paths broke it:

* the procedural generator loaded its map through a temp file and then deleted
  it, leaving ``file_path`` on the deleted temp file, the scene marked clean and
  the temp path in Recent Files — Ctrl+S "saved" into the temp directory and
  closing the editor discarded the map without a prompt;
* a load that failed after the old scene had been cleared left ``file_path`` on
  the *previous* map, so the next Ctrl+S wrote the half-built scene over it.
"""

import configparser
import json

import pytest

from editor.editor_state import EditorState

pytest.importorskip("PyQt5", reason="level loading lives on the editor window")


pytestmark = pytest.mark.qt


LEVEL = {"version": 3, "brushes": [], "things": []}


def test_a_generated_map_opens_untitled_and_unsaved(main_window):
    window = main_window
    window._on_procedural_map_generated(dict(LEVEL))

    assert window.file_path is None
    assert window.unsaved_changes


def test_a_map_file_opens_clean_under_its_own_path(main_window, tmp_path):
    path = tmp_path / "level.json"
    path.write_text(json.dumps(LEVEL))
    window = main_window

    assert window.load_level_file(str(path)) is True

    assert window.file_path == str(path)
    assert not window.unsaved_changes
    assert str(path) in window.recent_files


def test_a_load_failing_midway_detaches_the_previous_map(main_window, tmp_path, monkeypatch):
    path = tmp_path / "level.json"
    path.write_text(json.dumps(LEVEL))
    window = main_window
    def fail_apply(data):
        window.state.load_from_data(data)
        raise RuntimeError("entity failed to build")
    monkeypatch.setattr(window, "_apply_level_data", fail_apply)

    assert window.load_level_file(str(path)) is False

    assert window.file_path is None, (
        "a half-built scene is still attached to %r" % window.file_path)
    assert window.unsaved_changes
    assert window.ui.notification_label.text()


def test_an_unreadable_file_leaves_the_open_level_alone(main_window, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json")
    window = main_window
    window.file_path = "maps/previous.json"
    window.unsaved_changes = False

    assert window.load_level_file(str(path)) is False
    assert window.file_path == "maps/previous.json"
    assert not window.unsaved_changes


@pytest.mark.parametrize("document", [[], {"brushes": {"a": 1}}, {"things": ["x"]}])
def test_a_document_that_is_not_a_map_changes_nothing(main_window, tmp_path, document):
    path = tmp_path / "odd.json"
    path.write_text(json.dumps(document))
    window = main_window
    window.file_path = "maps/previous.json"
    window.unsaved_changes = False

    assert window.load_level_file(str(path)) is False
    assert window.file_path == "maps/previous.json"
    assert not window.unsaved_changes


def test_a_level_change_during_play_ends_the_session_before_the_swap(main_window, tmp_path):
    """LevelChanger loads the next map while play is running.

    The restart looked for an ``exit_play_mode`` that does not exist (the
    method is ``_exit_play_mode``) and fell back to flipping the view's flag,
    so the running session was never torn down: the logic thread was never
    told play had ended and the plugins never got ``on_play_stop``.  The
    teardown must also run *before* the scene is replaced, because it
    restores movers and doors by index into the world it started on.
    """
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    window = main_window
    window.view_3d.play_mode = True

    assert window.load_level_file(str(path)) is True
    assert not window.view_3d.play_mode


# ---------------------------------------------------------------------------
# What the player carries through a level change
# ---------------------------------------------------------------------------

@pytest.fixture
def playing_logic():
    """A real logic thread in play mode (not started: no tick runs)."""
    from engine.logic_thread import LogicThread
    from engine.player import Player
    from engine.threaded_game_state import ThreadedGameState

    logic = LogicThread(ThreadedGameState(), EditorState())
    logic.player_runtime.player = Player(0.0, 0.0)
    logic.session_runtime.apply_play_mode(True)
    yield logic
    logic.session_runtime.apply_play_mode(False)


def _window_on(main_window, logic, starts_play=True):
    window = main_window
    window.view_3d.play_mode = True

    original_exit = window._exit_play_mode
    original_enter = window.enter_play_mode

    def exit_play():
        original_exit()

    def enter_play():
        if starts_play:
            original_enter()
    window._exit_play_mode = exit_play
    window.enter_play_mode = enter_play
    window._original_play_methods = (original_exit, original_enter)
    window.view_3d.logic_thread = logic
    return window


def _loadout(logic):
    combat = logic.combat_runtime
    return (combat.active_weapon, combat.gun2_obtained, combat.player_ammo)


def test_the_player_keeps_their_weapons_through_a_level_change(main_window, tmp_path, playing_logic):
    """Ending play dropped the weapon and starting it again on the next map
    cleared it, so a LevelChanger always sent the player on unarmed."""
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun2"
    playing_logic.combat_runtime.gun2_obtained = True
    playing_logic.combat_runtime.player_ammo = 5
    window = _window_on(main_window, playing_logic)

    assert window.load_level_file(str(path)) is True

    assert playing_logic.session_runtime.play_mode
    assert _loadout(playing_logic) == ("gun2", True, 5)


def test_only_the_weapons_come_along(main_window, tmp_path, playing_logic):
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun1"
    playing_logic.player_runtime.collected_keys.add("blue_key")
    playing_logic.player_runtime.player_health = 40
    window = _window_on(main_window, playing_logic)

    window.load_level_file(str(path))

    assert playing_logic.combat_runtime.active_weapon == "gun1"
    assert playing_logic.player_runtime.collected_keys == set()
    assert playing_logic.player_runtime.player_health == 100


def test_a_level_that_does_not_restart_play_hands_nothing_back(main_window, tmp_path, playing_logic):
    """If play cannot restart (no PlayerStart), the weapons are not left
    waiting to reappear the next time Play is pressed."""
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun1"
    window = _window_on(main_window, playing_logic, starts_play=False)

    window.load_level_file(str(path))
    assert not playing_logic.session_runtime.play_mode
    playing_logic.session_runtime.apply_play_mode(True)

    assert _loadout(playing_logic) == (None, False, 0)


def test_stopping_and_starting_play_still_starts_unarmed(playing_logic):
    playing_logic.combat_runtime.active_weapon = "gun2"
    playing_logic.combat_runtime.gun2_obtained = True
    playing_logic.combat_runtime.player_ammo = 3
    playing_logic.session_runtime.apply_play_mode(False)
    playing_logic.session_runtime.apply_play_mode(True)
    assert _loadout(playing_logic) == (None, False, 0)


def test_a_map_with_a_player_start_recentres_now_and_once_deferred(main_window, tmp_path):
    """The real loader arms its deferred 2D-view recentering path."""
    level = {"version": 3, "brushes": [], "things": [
        {"type": "playerstart", "pos": [64, 0, 32],
         "properties": {"type": "playerstart", "name": "Start", "angle": 90}}]}
    path = tmp_path / "start.json"
    path.write_text(json.dumps(level))
    window = main_window
    assert window.load_level_file(str(path)) is True
    from PyQt5.QtWidgets import QApplication
    QApplication.processEvents()
    assert window.view_top is not None


def _load_into(window, data):
    from editor.things import Thing
    window.state.things = [Thing.from_dict(t) for t in data["things"]]
