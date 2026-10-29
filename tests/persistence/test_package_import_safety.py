"""A `.fiopak` must survive being played.

Tools -> Play Game Package streams a package without extracting and loads the
map directly from ZIP. The archive itself is an input, never an output: `save_level`
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
        # Each map is told apart by a marker brush: the editor keeps brushes,
        # not a map's free-form top-level fields.
        z.writestr("maps/a_other.json", json.dumps({"version": 3, "brushes": [
            {"id": "other-marker", "pos": [0, 0, 0], "size": [64, 64, 64]}],
            "things": []}))
        z.writestr("maps/z_start.json", json.dumps({"version": 3, "brushes": [
            {"id": "start-marker", "pos": [0, 0, 0], "size": [64, 64, 64]}],
            "things": []}))
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
    from engine.asset_archives import asset_bytes, mounted_game_package
    assert window._package_temp_dir is None
    assert mounted_game_package() == os.path.realpath(pak)
    assert asset_bytes("assets/sprites/pickup.png") == b"\\x89PNG not really"
    assert not (tmp_path / "assets").exists()
    window._discard_package_temp_dir()


def test_the_manifest_start_map_is_the_one_loaded(tmp_path, main_window):
    pak = _multi_map_package(str(tmp_path / "multi.fiopak"))
    window = main_window
    _prepare_package_window(window)

    window.play_package_from_path(pak)

    assert [b.get("id") for b in window.state.brushes] == ["start-marker"]
    window._discard_package_temp_dir()


def test_portable_packages_switch_without_creating_extraction_directories(
        tmp_path, main_window):
    from engine.asset_archives import asset_bytes, mounted_game_package
    first = make_package(str(tmp_path / "first.fiopak"))
    second = make_package(str(tmp_path / "second.fiopak"))
    window = main_window
    _prepare_package_window(window)

    window.play_package_from_path(first)
    assert mounted_game_package() == os.path.realpath(first)
    assert window._package_temp_dir is None
    assert asset_bytes("assets/sprites/pickup.png") == b"\\x89PNG not really"

    window.play_package_from_path(second)
    assert mounted_game_package() == os.path.realpath(second)
    assert window._package_temp_dir is None
    window._discard_package_temp_dir()
    assert mounted_game_package() is None
    assert not (tmp_path / "assets").exists()


def test_package_level_change_reads_map_without_extraction(tmp_path, main_window):
    pak = _multi_map_package(str(tmp_path / "levels.fiopak"))
    window = main_window
    _prepare_package_window(window)
    window.play_package_from_path(pak)
    try:
        assert window._package_map_path("maps/a_other.json") == "maps/a_other.json"
        assert window._package_map_path("../escape.json") is None
        assert window._load_package_map("maps/a_other.json")
        assert [b.get("id") for b in window.state.brushes] == ["other-marker"]
        assert window._package_temp_dir is None
        assert not (tmp_path / "maps").exists()
    finally:
        window._discard_package_temp_dir()


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


def test_over_budget_package_is_refused_before_loading(tmp_path, main_window, monkeypatch):
    """The package mount checks the total declared expansion before playback."""
    import player.fiopak as fiopak
    from engine.asset_archives import mounted_game_package
    monkeypatch.setattr(fiopak, "MAX_TOTAL_ASSET_BYTES", 1024 * 1024)
    pak = str(tmp_path / "bomb.fiopak")
    with zipfile.ZipFile(pak, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("metadata.json", json.dumps({"map_path": "maps/level.json"}))
        z.writestr("maps/level.json", json.dumps({
            "version": 3, "brushes": [], "things": []}))
        for i in range(4):
            z.writestr("assets/pad%d.bin" % i, b"\\0" * (512 * 1024))
    window = main_window
    _prepare_package_window(window)
    window.play_package_from_path(pak)
    assert mounted_game_package() is None
    assert window._package_temp_dir is None
    assert not (tmp_path / "assets").exists()
