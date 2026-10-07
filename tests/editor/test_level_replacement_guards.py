"""Every way the open level can be replaced keeps the user's unsaved work.

File > Open, New and closing the window asked before discarding edits; four
other routes did not:

* a Recent Files entry and the console ``map`` / ``load`` commands opened the
  map straight away;
* a LevelChanger (or a map's ``map`` command) changed level in Play and the
  edited level was simply gone;
* the procedural generator replaced whatever was open with its output.

In the editor they now ask, as File > Open does. In Play there is no moment to
ask, so the edited level is written to its autosave file before it is left.

A played ``.fiopak`` resolves its own level changes: ``maps/next.json`` is the
package's map, not one in the editor's ``maps/`` folder.
"""

import json
import os

import pytest

pytest.importorskip("PyQt5", reason="level replacement lives on the editor window")

from editor.things import PlayerStart                   # noqa: E402
from tests.helpers.worlds import box_brush              # noqa: E402

pytestmark = pytest.mark.qt


LEVEL = {"version": 3, "brushes": [], "things": []}
PLAYABLE = {"version": 3, "brushes": [], "things": [
    {"type": "playerstart", "pos": [0.0, 0.0, 0.0],
     "properties": {"type": "playerstart", "name": "PlayerStart_1"}}]}


def _write_map(path, level=LEVEL):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(level))
    return str(path)


@pytest.fixture
def prompts(main_window, monkeypatch):
    """Record unsaved-changes prompts; the user answers Cancel."""
    asked = []

    def cancel():
        if not main_window.unsaved_changes:
            return True                 # nothing to ask (fixture teardown)
        asked.append(True)
        return False

    monkeypatch.setattr(main_window, "check_unsaved_changes", cancel)
    return asked


def _edited_level(window, tmp_path):
    original = _write_map(tmp_path / "maps" / "original.json")
    assert window.load_level_file(original)
    window.state.brushes.append(box_brush("wall", (0, 64, 0)))
    window.unsaved_changes = True
    return original


def test_a_recent_file_asks_before_replacing_unsaved_work(main_window, tmp_path, prompts):
    window = main_window
    original = _edited_level(window, tmp_path)
    other = _write_map(tmp_path / "maps" / "other.json")
    window.recent_files = [other]
    window.update_recent_files_menu()

    window.recent_menu.actions()[0].trigger()

    assert prompts == [True]
    assert window.file_path == original
    assert window.unsaved_changes


def test_a_level_change_in_the_editor_asks_first(main_window, tmp_path, prompts):
    window = main_window
    original = _edited_level(window, tmp_path)
    other = _write_map(tmp_path / "maps" / "other.json")

    window.load_level_signal.emit(other, "")

    assert prompts == [True]
    assert window.file_path == original


def test_the_console_map_command_asks_first(main_window, tmp_path, prompts, monkeypatch):
    window = main_window
    monkeypatch.setattr(window, "root_dir", str(tmp_path))
    original = _edited_level(window, tmp_path)
    _write_map(tmp_path / "maps" / "other.json")

    window.console_handler.handle_command("map other")

    assert prompts == [True]
    assert window.file_path == original


@pytest.mark.parametrize("restore_on_stop", [False, True])
def test_a_level_change_in_play_keeps_the_edits_in_the_autosave(
        main_window, tmp_path, monkeypatch, restore_on_stop):
    window = main_window
    monkeypatch.setattr(window, "root_dir", str(tmp_path))
    if not window.config.has_section("Settings"):
        window.config.add_section("Settings")
    window.config.set("Settings", "restore_world_on_stop", str(restore_on_stop))
    _edited_level(window, tmp_path)
    window.state.things.append(PlayerStart(pos=[0, 0, 0]))
    other = _write_map(tmp_path / "maps" / "other.json", PLAYABLE)

    window.enter_play_mode()
    assert window.view_3d.play_mode
    asked = []
    monkeypatch.setattr(window, "check_unsaved_changes",
                        lambda: asked.append(True) or True)
    window.state.brushes[0]["hidden"] = True            # what the game did

    assert window.open_level_file(other) is True

    assert asked == [], "Play cannot stop to ask"

    assert window.file_path == other
    saved = json.loads((tmp_path / "maps" / "original_autosave.json").read_text())
    walls = [b for b in saved["brushes"] if b.get("name") == "wall"]
    assert len(walls) == 1
    # With restore-on-stop the autosave holds the world as edited, not as played.
    assert bool(walls[0].get("hidden")) is not restore_on_stop
    window._exit_play_mode()


def test_regenerating_replaces_the_generators_own_preview_without_asking(
        main_window, prompts):
    window = main_window
    window._on_procedural_map_generated(dict(LEVEL))
    assert window.unsaved_changes

    window._on_procedural_map_generated(dict(LEVEL))

    assert prompts == []


def test_generating_asks_before_replacing_unsaved_work(main_window, tmp_path, prompts):
    window = main_window
    original = _edited_level(window, tmp_path)

    window._on_procedural_map_generated(dict(LEVEL))

    assert prompts == [True]
    assert window.file_path == original


def test_generating_asks_once_the_preview_has_been_edited(main_window, prompts):
    window = main_window
    window._on_procedural_map_generated(dict(LEVEL))
    window.state.brushes.append(box_brush("wall", (0, 64, 0)))
    window.state.save_state()

    window._on_procedural_map_generated(dict(LEVEL))

    assert prompts == [True]


def test_a_played_package_changes_to_its_own_maps(main_window, tmp_path):
    window = main_window
    package = tmp_path / "package"
    _write_map(package / "maps" / "next.json",
               {"version": 3, "brushes": [box_brush("pkg", (0, 0, 0))], "things": []})
    window._package_level_dir = str(package)

    assert window.open_level_file("maps/next.json") is True

    assert [b.get("name") for b in window.state.brushes] == ["pkg"]
    # Never the extraction's path: Ctrl+S must not write into the temp folder.
    assert window.file_path is None
    assert not window.unsaved_changes
    assert window._package_level_dir == str(package)


def test_a_package_level_change_cannot_climb_out_of_the_package(main_window, tmp_path):
    window = main_window
    package = tmp_path / "package"
    package.mkdir()
    _write_map(tmp_path / "outside.json")
    window._package_level_dir = str(package)

    assert window._package_map_path("../outside.json") is None


def test_opening_a_map_file_ends_the_package_session(main_window, tmp_path):
    window = main_window
    window._package_level_dir = str(tmp_path)
    assert window.load_level_file(_write_map(tmp_path / "maps" / "plain.json"))
    assert window._package_level_dir is None


@pytest.mark.parametrize("command", ["vsync", "r_vsync", "fps"])
def test_map_logic_does_not_write_settings(main_window, monkeypatch, command):
    window = main_window
    writes = []
    monkeypatch.setattr(window, "save_config", lambda: writes.append(True))
    before = window.config.get("Display", "vsync", fallback=None)

    window.console_handler.handle_command(command, from_map=True)

    assert writes == []
    assert window.config.get("Display", "vsync", fallback=None) == before


def test_a_played_packages_map_command_changes_to_its_own_maps(main_window, tmp_path):
    window = main_window
    package = tmp_path / "package"
    _write_map(package / "maps" / "next.json",
               {"version": 3, "brushes": [box_brush("pkg", (0, 0, 0))], "things": []})
    window._package_level_dir = str(package)

    window.console_handler.handle_command("map next", from_map=True)

    assert [b.get("name") for b in window.state.brushes] == ["pkg"]
    assert window.file_path is None


# ---------------------------------------------------------------------------
# A level with no Player Start, reached during Play
# ---------------------------------------------------------------------------

def _playing_on(window, tmp_path):
    current = _write_map(tmp_path / "maps" / "current.json", PLAYABLE)
    assert window.load_level_file(current)
    window.enter_play_mode()
    assert window.view_3d.play_mode
    return current


def test_a_game_level_change_to_a_map_without_a_start_is_refused(
        main_window, tmp_path, modal_dialogs, monkeypatch):
    """The same authoring error as a missing destination spawn: Play carries
    on where it is, with a toast, not a dialog that blocks the game loop."""
    window = main_window
    current = _playing_on(window, tmp_path)
    toasts = []
    monkeypatch.setattr(window, "show_toast",
                        lambda text, is_error=False: toasts.append((text, is_error)))
    things_before = list(window.state.things)
    dialogs_before = len(modal_dialogs)
    startless = _write_map(tmp_path / "maps" / "startless.json")

    window.load_level_signal.emit(startless, "")

    assert window.view_3d.play_mode
    assert window.file_path == current
    assert window.state.things == things_before
    assert toasts == [(f"Level change: '{startless}' has no Player Start", True)]
    assert len(modal_dialogs) == dialogs_before
    window._exit_play_mode()


def test_map_logics_map_command_to_a_map_without_a_start_is_refused(
        main_window, tmp_path, monkeypatch):
    window = main_window
    monkeypatch.setattr(window, "root_dir", str(tmp_path))
    current = _playing_on(window, tmp_path)
    _write_map(tmp_path / "maps" / "startless.json")

    window.console_handler.handle_command("map startless", from_map=True)

    assert window.view_3d.play_mode
    assert window.file_path == current
    window._exit_play_mode()


def test_a_user_opening_a_map_without_a_start_in_play_gets_no_dialog(
        main_window, tmp_path, modal_dialogs, monkeypatch):
    """The user asked for it, so it opens, and Play ends with a toast."""
    window = main_window
    _playing_on(window, tmp_path)
    toasts = []
    monkeypatch.setattr(window, "show_toast",
                        lambda text, is_error=False: toasts.append((text, is_error)))
    dialogs_before = len(modal_dialogs)
    startless = _write_map(tmp_path / "maps" / "startless.json")

    assert window.open_level_file(startless) is True

    assert window.file_path == startless
    assert not window.view_3d.play_mode
    assert toasts[-1] == ("Loaded startless.json; Play stopped: it has no Player Start",
                          True)
    assert len(modal_dialogs) == dialogs_before


def test_play_tells_the_logic_thread_where_a_played_package_is(main_window, tmp_path):
    """Cutscenes inside a package are looked up in its extraction."""
    window = main_window
    package = tmp_path / "package"
    _write_map(package / "maps" / "start.json", PLAYABLE)
    window._package_level_dir = str(package)
    assert window.open_level_file("maps/start.json")

    window.enter_play_mode()
    assert window.view_3d.logic_thread.package_root == str(package)
    window._exit_play_mode()

    window._package_level_dir = None
    window.enter_play_mode()
    assert window.view_3d.logic_thread.package_root is None
    window._exit_play_mode()


def test_an_exported_packages_cutscene_plays_from_the_package(main_window, tmp_path):
    """Export a map whose LogicCamera names a cutscene, play the package on an
    editor that has no such cutscene, and the cutscene still resolves."""
    from editor.package_exporter import PackageExporter

    project = tmp_path / "project"
    cutscene = {"version": 2, "name": "intro", "actors": [],
                "camera": [{"time": 0.0, "pos": [0, 0, 0], "yaw": 0, "pitch": 0}]}
    _write_map(project / "cutscenes" / "intro.json", cutscene)
    level = json.loads(json.dumps(PLAYABLE))
    level["things"].append({"type": "logic_camera", "pos": [0, 0, 0], "properties": {
        "type": "logic_camera", "cutscene_file": "cutscenes/intro.json"}})
    current = _write_map(project / "maps" / "start.json", level)
    pak = tmp_path / "game.fiopak"
    ok, errors = PackageExporter(None, str(project)).export(
        str(pak), {"title": "T"}, current)
    assert ok and errors == [], errors

    window = main_window
    window.config["Kiosk"] = {"launch_in_editor": "true"}
    window.play_package_from_path(str(pak))
    window.enter_play_mode()
    try:
        runtime = window.view_3d.logic_thread.cutscene_runtime
        found = runtime._cutscene_file_path("cutscenes/intro.json")
        assert found is not None
        assert found.startswith(os.path.realpath(window._package_temp_dir))
        assert runtime._load_cutscene_file("cutscenes/intro.json")["name"] == "intro"
    finally:
        window._exit_play_mode()
        window._discard_package_temp_dir()


def test_the_cutscene_lookup_prefers_the_played_package(main_window, tmp_path):
    logic = main_window.view_3d.logic_thread
    runtime = logic.cutscene_runtime
    package = tmp_path / "package"
    cutscene = {"version": 2, "actors": [], "camera": []}
    _write_map(package / "cutscenes" / "showcase.json", dict(cutscene, name="pkg"))
    _write_map(package / "cutscenes" / "only_here.json", cutscene)
    (package / "outside.json").write_text("{}")

    logic.package_root = str(package)
    try:
        assert runtime._cutscene_file_path("cutscenes/showcase.json") == \
            os.path.realpath(package / "cutscenes" / "showcase.json")
        assert runtime._cutscene_file_path("cutscenes/only_here.json")
        assert runtime._cutscene_file_path("cutscenes/../outside.json") is None
    finally:
        logic.package_root = None
    # Without a package: the project's own cutscenes, and nothing else.
    assert runtime._cutscene_file_path("cutscenes/showcase.json").endswith(
        os.path.join("cutscenes", "showcase.json"))
    assert not runtime._cutscene_file_path("cutscenes/showcase.json").startswith(
        os.path.realpath(package))
    assert runtime._cutscene_file_path("cutscenes/only_here.json") is None
