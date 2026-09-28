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
        self.state = types.SimpleNamespace(things=[])
        self.view_3d = types.SimpleNamespace(
            play_mode=False, camera=types.SimpleNamespace())
        self.fail_apply = fail_apply
        self.applied = []
        self.recent = []
        self.toasts = []

    def _apply_level_data(self, level_data):
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
