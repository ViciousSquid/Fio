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
import types

import pytest

pytest.importorskip("PyQt5", reason="level loading lives on the editor window")

from editor.editor_state import EditorState        # noqa: E402
from editor.main_window import MainWindow          # noqa: E402

pytestmark = pytest.mark.qt


class _Window:
    """The slice of MainWindow a level load touches; loading logic is real."""

    load_level_file = MainWindow.load_level_file
    _load_level = MainWindow._load_level
    _on_procedural_map_generated = MainWindow._on_procedural_map_generated

    def __init__(self, fail_apply=False):
        self.file_path = "maps/previous.json"
        self.unsaved_changes = False
        self.config = configparser.ConfigParser()
        self.state = types.SimpleNamespace(
            things=[], validate_level_data=EditorState.validate_level_data)
        self.view_3d = types.SimpleNamespace(
            play_mode=False, camera=types.SimpleNamespace())
        self.fail_apply = fail_apply
        self.applied = []
        self.recent = []
        self.toasts = []
        self.calls = []

    def _apply_level_data(self, level_data):
        self.calls.append("replace scene")
        self.applied.append(level_data)
        if self.fail_apply:
            raise RuntimeError("entity failed to build")

    def add_recent_file(self, path):
        self.recent.append(path)

    def show_toast(self, message, is_error=False, **_kw):
        self.toasts.append((message, is_error))

    def update_title(self):
        pass

    def set_selected_object(self, obj):
        pass

    def update_all_ui(self):
        pass

    def _refresh_logic_graph(self):
        pass

    def _close_current_overlay(self):
        pass

    def _exit_play_mode(self):
        self.calls.append("exit play")
        self.view_3d.play_mode = False

    def enter_play_mode(self):
        self.calls.append("enter play")
        self.view_3d.play_mode = True

    def center_2d_views_on(self, world_pos):
        # _load_level calls this now and again from a 50 ms timer for a map
        # with a PlayerStart; the host must serve the deferred call too.
        self.calls.append("centre views")


LEVEL = {"version": 3, "brushes": [], "things": []}


def test_a_generated_map_opens_untitled_and_unsaved():
    window = _Window()

    window._on_procedural_map_generated(dict(LEVEL))

    assert window.applied == [LEVEL]
    assert window.file_path is None, (
        "generated map is attached to %r; Ctrl+S would write there"
        % window.file_path)
    assert window.unsaved_changes, "closing would discard the generated map"
    assert window.recent == []


def test_a_map_file_opens_clean_under_its_own_path(tmp_path):
    path = tmp_path / "level.json"
    path.write_text(json.dumps(LEVEL))
    window = _Window()

    assert window.load_level_file(str(path)) is True

    assert window.file_path == str(path)
    assert not window.unsaved_changes
    assert window.recent == [str(path)]


def test_a_load_failing_midway_detaches_the_previous_map(tmp_path):
    path = tmp_path / "level.json"
    path.write_text(json.dumps(LEVEL))
    window = _Window(fail_apply=True)

    assert window.load_level_file(str(path)) is False

    assert window.file_path is None, (
        "a half-built scene is still attached to %r" % window.file_path)
    assert window.unsaved_changes
    assert window.toasts[-1][1]


def test_an_unreadable_file_leaves_the_open_level_alone(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json")
    window = _Window()

    assert window.load_level_file(str(path)) is False

    assert window.applied == [], "the scene was touched for a file never parsed"
    assert window.file_path == "maps/previous.json"
    assert not window.unsaved_changes


@pytest.mark.parametrize("document", [[], {"brushes": {"a": 1}}, {"things": ["x"]}])
def test_a_document_that_is_not_a_map_changes_nothing(tmp_path, document):
    path = tmp_path / "odd.json"
    path.write_text(json.dumps(document))
    window = _Window()

    assert window.load_level_file(str(path)) is False

    assert window.applied == []
    assert window.file_path == "maps/previous.json"
    assert not window.unsaved_changes


def test_a_level_change_during_play_ends_the_session_before_the_swap(tmp_path):
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
    window = _Window()
    window.view_3d.play_mode = True

    assert window.load_level_file(str(path)) is True

    assert window.calls == ["exit play", "replace scene", "enter play"]


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
    logic.player = Player(0.0, 0.0)
    logic.session_runtime.apply_play_mode(True)
    yield logic
    logic.session_runtime.apply_play_mode(False)


def _window_on(logic, starts_play=True):
    """The window, with play stopped and started on the real logic thread."""
    window = _Window()
    window.view_3d.play_mode = True
    window.view_3d.logic_thread = logic

    def exit_play():
        window.calls.append("exit play")
        logic.session_runtime.apply_play_mode(False)
        window.view_3d.play_mode = False

    def enter_play():
        window.calls.append("enter play")
        if starts_play:              # a map without a PlayerStart does not
            logic.session_runtime.apply_play_mode(True)
            window.view_3d.play_mode = True

    window._exit_play_mode = exit_play
    window.enter_play_mode = enter_play
    return window


def _loadout(logic):
    combat = logic.combat_runtime
    return (combat.active_weapon, combat.gun2_obtained, combat.player_ammo)


def test_the_player_keeps_their_weapons_through_a_level_change(tmp_path, playing_logic):
    """Ending play dropped the weapon and starting it again on the next map
    cleared it, so a LevelChanger always sent the player on unarmed."""
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun2"
    playing_logic.combat_runtime.gun2_obtained = True
    playing_logic.combat_runtime.player_ammo = 5
    window = _window_on(playing_logic)

    assert window.load_level_file(str(path)) is True

    assert window.calls == ["exit play", "replace scene", "enter play"]
    assert playing_logic.play_mode
    assert _loadout(playing_logic) == ("gun2", True, 5)


def test_only_the_weapons_come_along(tmp_path, playing_logic):
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun1"
    playing_logic.player_runtime.collected_keys.add("blue_key")
    playing_logic.player_health = 40
    window = _window_on(playing_logic)

    window.load_level_file(str(path))

    assert playing_logic.combat_runtime.active_weapon == "gun1"
    assert playing_logic.player_runtime.collected_keys == set()
    assert playing_logic.player_health == 100


def test_a_level_that_does_not_restart_play_hands_nothing_back(tmp_path, playing_logic):
    """If play cannot restart (no PlayerStart), the weapons are not left
    waiting to reappear the next time Play is pressed."""
    path = tmp_path / "next.json"
    path.write_text(json.dumps(LEVEL))
    playing_logic.combat_runtime.active_weapon = "gun1"
    window = _window_on(playing_logic, starts_play=False)

    window.load_level_file(str(path))
    assert not playing_logic.play_mode
    playing_logic.session_runtime.apply_play_mode(True)

    assert _loadout(playing_logic) == (None, False, 0)


def test_stopping_and_starting_play_still_starts_unarmed(playing_logic):
    playing_logic.combat_runtime.active_weapon = "gun2"
    playing_logic.combat_runtime.gun2_obtained = True
    playing_logic.combat_runtime.player_ammo = 3
    playing_logic.session_runtime.apply_play_mode(False)
    playing_logic.session_runtime.apply_play_mode(True)
    assert _loadout(playing_logic) == (None, False, 0)


def test_a_map_with_a_player_start_recentres_now_and_once_deferred(tmp_path):
    """The load arms a 50 ms timer that re-centres the 2D views; the stand-in
    host has to serve that deferred call (the suite's teardown guard runs it
    and fails this test if it cannot)."""
    level = {"version": 3, "brushes": [], "things": [
        {"type": "playerstart", "pos": [64, 0, 32],
         "properties": {"type": "playerstart", "name": "Start", "angle": 90}}]}
    path = tmp_path / "start.json"
    path.write_text(json.dumps(level))
    window = _Window()
    window._apply_level_data = lambda data: _load_into(window, data)

    assert window.load_level_file(str(path)) is True
    assert window.calls.count("centre views") == 1


def _load_into(window, data):
    from editor.things import Thing
    window.state.things = [Thing.from_dict(t) for t in data["things"]]
