"""A `.fiopak` must survive being played.

Tools -> Play Game Package extracts a package to a temp directory and loads the
map out of it. The archive itself is an input, never an output: `save_level`
writes `self.file_path` with `json.dump`, so leaving `file_path` pointing at the
package means one Ctrl+S replaces a ZIP -- the map, every texture, every model,
every bundled plugin -- with a bare JSON file.

Both import paths used to do exactly that, twenty-five lines below a comment
saying not to.
"""

import json
import os
import re
import zipfile

import pytest

pytest.importorskip("PyQt5", reason="the import workflow is editor-tier")

from editor.main_window import MainWindow          # noqa: E402

pytestmark = pytest.mark.qt


def make_package(path):
    """A package shaped like PackageExporter writes one."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("metadata.json", json.dumps({"title": "Demo",
                                                "map_path": "maps/level.json"}))
        z.writestr("maps/level.json", json.dumps({"version": 3, "brushes": [],
                                                  "things": []}))
        z.writestr("assets/sprites/pickup.png", b"\x89PNG not really")
    return path


def test_saving_over_a_package_uses_the_real_editor_writer(tmp_path, main_window):
    """The package overwrite path is exercised through a real MainWindow."""
    pak = make_package(str(tmp_path / "demo.fiopak"))
    assert zipfile.is_zipfile(pak)

    main_window.file_path = pak
    main_window.unsaved_changes = True
    main_window.save_level()

    assert not zipfile.is_zipfile(pak), (
        "this test is a regression reproducer for the raw save writer: "
        "save_level() currently treats its file_path as a JSON level path")


def _multi_map_package(path):
    """Two maps; the manifest names the one that does not sort first."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("metadata.json", json.dumps({"title": "Demo",
                                                "map_path": "maps/z_start.json"}))
        z.writestr("maps/a_other.json", json.dumps({"version": 3, "name": "other",
                                                    "brushes": [], "things": []}))
        z.writestr("maps/z_start.json", json.dumps({"version": 3, "name": "start",
                                                    "brushes": [], "things": []}))
    return path


def _prepare_package_window(window):
    window.config["Kiosk"] = {"launch_in_editor": "true"}
    window.unsaved_changes = False
    window.file_path = None


def test_playing_a_package_never_leaves_file_path_on_it(tmp_path, main_window):
    pak = make_package(str(tmp_path / "demo.fiopak"))
    before = open(pak, "rb").read()
    window = main_window
    _prepare_package_window(window)

    window.play_package_from_path(pak)

    assert window.state.things == []
    assert window.state.brushes == []
    assert window.file_path is None, (
        "file_path points at %r; save_level() would write that path"
        % window.file_path)
    assert open(pak, "rb").read() == before, "the archive itself was modified"
    assert window._package_temp_dir is not None
    window._discard_package_temp_dir()


def test_the_manifest_start_map_is_the_one_loaded(tmp_path, main_window):
    pak = _multi_map_package(str(tmp_path / "multi.fiopak"))
    window = main_window
    _prepare_package_window(window)

    window.play_package_from_path(pak)

    assert window.state.get_level_data()["name"] == "start"
    window._discard_package_temp_dir()


def test_the_extraction_is_a_temp_copy_released_by_the_next_package(
        tmp_path, main_window):
    first = make_package(str(tmp_path / "first.fiopak"))
    second = make_package(str(tmp_path / "second.fiopak"))
    window = main_window
    _prepare_package_window(window)

    window.play_package_from_path(first)
    first_dir = window._package_temp_dir
    assert first_dir and os.path.isfile(
        os.path.join(first_dir, "assets", "sprites", "pickup.png"))
    assert not first_dir.startswith(str(tmp_path))

    window.play_package_from_path(second)
    assert not os.path.exists(first_dir), "the previous extraction leaked"
    second_dir = window._package_temp_dir
    window._discard_package_temp_dir()
    assert not os.path.exists(second_dir)


@pytest.mark.parametrize("entries", [
    {"../escape.json": "{}"},
    {"plugins/evil/__init__.py": "raise SystemExit"},
], ids=["path-traversal", "bundled-plugin-code"])
def test_a_hostile_package_is_refused_before_the_scene_changes(
        tmp_path, entries, main_window):
    pak = str(tmp_path / "hostile.fiopak")
    with zipfile.ZipFile(pak, "w") as z:
        z.writestr("metadata.json", json.dumps({"map_path": "maps/level.json"}))
        z.writestr("maps/level.json", json.dumps({
            "version": 3, "brushes": [], "things": []}))
        for name, data in entries.items():
            z.writestr(name, data)

    window = main_window
    _prepare_package_window(window)
    window.file_path = str(tmp_path / "maps" / "previous.json")

    window.play_package_from_path(pak)

    assert window.state.brushes == []
    assert window.state.things == []
    assert window.file_path.endswith("previous.json")
    assert getattr(window, "_package_temp_dir", None) is None
    assert window.unsaved_changes is False
    assert not (tmp_path / "escape.json").exists()
