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


def test_no_import_path_leaves_file_path_pointing_at_the_package():
    """Read as source: driving the real importer needs a live editor window.

    Every assignment of ``self.file_path`` inside the two package-import
    functions must be ``None``. A first save then goes through Save As, which
    is what the comment in those functions has always promised.
    """
    source = open("editor/main_window.py", encoding="utf-8").read()

    for func in ("play_game_package", "play_package_from_path"):
        start = source.index("def %s(self" % func)
        end = source.index("\n    def ", start + 1)
        body = source[start:end]
        assigned = re.findall(r"self\.file_path\s*=\s*(.+)", body)
        assert assigned, "%s no longer assigns file_path at all" % func
        for value in assigned:
            assert value.strip() == "None", (
                "%s sets self.file_path = %s; save_level() writes that path, so "
                "the package is overwritten on the first Ctrl+S"
                % (func, value.strip()))


def test_the_importers_still_extract_to_a_temp_directory():
    """The archive is read-only input; the working copy is the extraction."""
    source = open("editor/main_window.py", encoding="utf-8").read()
    for func in ("play_game_package", "play_package_from_path"):
        start = source.index("def %s(self" % func)
        end = source.index("\n    def ", start + 1)
        body = source[start:end]
        assert "tempfile.mkdtemp" in body, (
            "%s no longer extracts to a temp directory" % func)
        assert "_safe_extract_zip" in body, (
            "%s extracts without the path-traversal guard" % func)
