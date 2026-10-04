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


class SaveStub:
    """The slice of MainWindow that `save_level` touches."""

    def __init__(self, file_path):
        self.file_path = file_path
        self.unsaved_changes = True
        self.saved_as_called = False
        self.state = type("S", (), {
            "get_level_data": staticmethod(
                lambda: {"version": 3, "brushes": [], "things": []})
        })()

    def save_level_as(self):
        self.saved_as_called = True

    def stop_mover_preview(self):
        pass

    def update_title(self):
        pass

    def add_recent_file(self, path):
        pass

    def show_toast(self, *args, **kwargs):
        pass


def test_saving_over_a_package_would_destroy_it(tmp_path):
    """The failure this guards against, demonstrated on a real archive."""
    pak = make_package(str(tmp_path / "demo.fiopak"))
    assert zipfile.is_zipfile(pak)

    MainWindow.save_level(SaveStub(pak))

    assert not zipfile.is_zipfile(pak), (
        "this test no longer demonstrates anything — if save_level has grown a "
        "guard of its own, assert on that instead")


class PlayStub:
    """The slice of MainWindow that playing a package touches.

    The package-handling methods are the real ones; only the scene and the
    chrome around it are recorded instead of built.
    """

    _extract_package = MainWindow._extract_package
    _safe_extract_zip = MainWindow._safe_extract_zip
    _discard_package_temp_dir = MainWindow._discard_package_temp_dir
    play_package_from_path = MainWindow.play_package_from_path

    def __init__(self, file_path="maps/previous.json"):
        import configparser
        self.file_path = file_path
        self.unsaved_changes = False
        self.config = configparser.ConfigParser()
        self.config["Kiosk"] = {"launch_in_editor": "true"}
        self.applied = []
        self.toasts = []

    def check_unsaved_changes(self):
        return True

    def _apply_level_data(self, level_data):
        # What the scene is built from must be a copy, not the archive.
        self.applied.append(level_data)

    def show_toast(self, message, is_error=False, **kwargs):
        self.toasts.append((message, is_error))

    def update_title(self):
        pass

    def set_selected_objects(self, objects):
        pass

    def update_all_ui(self):
        pass

    def enter_kiosk_mode(self):
        raise AssertionError("launch_in_editor is set")


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


def test_playing_a_package_never_leaves_file_path_on_it(tmp_path):
    pak = make_package(str(tmp_path / "demo.fiopak"))
    before = open(pak, "rb").read()
    window = PlayStub()

    window.play_package_from_path(pak)

    assert window.applied, window.toasts
    assert window.file_path is None, (
        "file_path points at %r; save_level() writes that path, so the first "
        "Ctrl+S would overwrite it" % window.file_path)
    assert open(pak, "rb").read() == before, "the archive itself was modified"
    window._discard_package_temp_dir()


def test_the_manifest_start_map_is_the_one_loaded(tmp_path):
    pak = _multi_map_package(str(tmp_path / "multi.fiopak"))
    window = PlayStub()

    window.play_package_from_path(pak)

    assert [level["name"] for level in window.applied] == ["start"]
    window._discard_package_temp_dir()


def test_the_extraction_is_a_temp_copy_released_by_the_next_package(tmp_path):
    first = make_package(str(tmp_path / "first.fiopak"))
    second = make_package(str(tmp_path / "second.fiopak"))
    window = PlayStub()

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
def test_a_hostile_package_is_refused_before_the_scene_changes(tmp_path, entries):
    pak = str(tmp_path / "hostile.fiopak")
    with zipfile.ZipFile(pak, "w") as z:
        z.writestr("metadata.json", json.dumps({"map_path": "maps/level.json"}))
        z.writestr("maps/level.json", json.dumps({"version": 3}))
        for name, data in entries.items():
            z.writestr(name, data)
    window = PlayStub()

    window.play_package_from_path(pak)

    assert window.applied == []
    assert window.file_path == "maps/previous.json"
    assert getattr(window, "_package_temp_dir", None) is None
    assert window.toasts and window.toasts[-1][1], window.toasts
    assert not (tmp_path / "escape.json").exists()
