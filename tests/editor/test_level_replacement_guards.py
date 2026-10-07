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
