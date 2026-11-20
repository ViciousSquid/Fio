"""How the editor writes a map, and what an unbuildable map does to the open one.

* Autosave (audit P20): the docstring said "Background autosave" but it
  encoded and wrote the whole map on the UI thread. The level is still
  serialized on the UI thread (so the file is one consistent moment of it),
  and the file is written on a worker. A save and an autosave never write at
  once (one lock), and an older autosave never lands over a newer one.
* NaN (audit D2): ``json.dumps`` wrote ``NaN``, the save "worked", and the map
  then failed validation on load. Saving now refuses, saying where the value
  is, while the file on disk is still the good one.
* A terrain that cannot be built (audit S9) refused the map only after the
  scene had been replaced: a half-built untitled level. It is now built
  before the open level is touched.
* Progress dialogs (audit M4) are deleted when they close, not left behind
  as hidden children, one per terrain operation.
"""

import json
import math
import threading

import pytest
from PyQt5.QtCore import QEvent

from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt


@pytest.fixture
def window(main_window, tmp_path, monkeypatch):
    monkeypatch.setattr(main_window, "root_dir", str(tmp_path))
    (tmp_path / "maps").mkdir(exist_ok=True)
    main_window.state.brushes.append(box_brush("wall", (0, 0, 0)))
    main_window.file_path = str(tmp_path / "maps" / "level.json")
    main_window.unsaved_changes = True
    toasts = []
    monkeypatch.setattr(main_window, "show_toast", lambda message, is_error=False, **_kw:
                        toasts.append((str(message), is_error)))
    main_window.toasts = toasts
    yield main_window
    main_window.wait_for_autosave(timeout=10)


def test_autosave_writes_on_a_worker_what_the_scene_was_when_it_ran(window, tmp_path,
                                                                     monkeypatch):
    import editor.main_window as main_window_module
    real_write = main_window_module.write_text_atomic
    release = threading.Event()
    writers = []

    def gated_write(path, text, *args, **kwargs):
        writers.append((threading.current_thread() is threading.main_thread(),
                        window._map_io_lock.locked()))
        assert release.wait(10)
        return real_write(path, text, *args, **kwargs)
    monkeypatch.setattr(main_window_module, "write_text_atomic", gated_write)

    window.autosave()                       # returns with the write still pending
    window.state.brushes[0]["name"] = "renamed after the autosave"
    release.set()
    assert window.wait_for_autosave(timeout=10)

    saved = json.loads((tmp_path / "maps" / "level_autosave.json").read_text())
    assert [b["name"] for b in saved["brushes"]] == ["wall"]
    assert writers == [(False, True)], "written off the UI thread, under the map lock"
    assert window.unsaved_changes, "an autosave is not the level's file"


def test_an_older_autosave_never_lands_over_a_newer_one(window, tmp_path):
    path = str(tmp_path / "maps" / "level_autosave.json")
    older = window._map_texts(minified=True)
    window.state.brushes[0]["name"] = "newer"
    newer = window._map_texts(minified=True)
    window._commit_map_texts(path, newer, autosave_generation=2)
    window._commit_map_texts(path, older, autosave_generation=1)
    saved = json.loads(open(path).read())
    assert [b["name"] for b in saved["brushes"]] == ["newer"]


def test_a_level_change_autosave_is_written_before_it_returns(window, tmp_path):
    path = window._write_autosave()
    assert path and json.loads(open(path).read())["brushes"][0]["name"] == "wall"


def test_a_nan_coordinate_refuses_the_save_and_keeps_the_file(window, tmp_path):
    target = tmp_path / "maps" / "level.json"
    window.save_level()
    good = target.read_text()
    window.state.brushes[0]["pos"][1] = math.nan
    window.save_level()
    assert target.read_text() == good, "a map with NaN in it replaced the good file"
    message = window.toasts[-1][0]
    assert "not a number" in message and "wall" in message and "pos[1]" in message
    with pytest.raises(ValueError):
        window._write_map(str(tmp_path / "maps" / "other.json"))
    assert not (tmp_path / "maps" / "other.json").exists()


def test_a_map_whose_terrain_cannot_be_built_leaves_the_open_level(window, tmp_path,
                                                                   monkeypatch):
    from engine import terrain as terrain_module
    window.save_level()
    brushes, things = window.state.brushes, window.state.things
    level = {"version": 3, "brushes": [], "things": [],
             "terrain_data": {"seed": 7}}
    other = tmp_path / "maps" / "terrain.json"
    other.write_text(json.dumps(level))

    def broken(self, data):
        raise TypeError("seed must be an int")
    monkeypatch.setattr(terrain_module.Terrain, "from_dict", broken)

    assert window.load_level_file(str(other)) is False
    assert window.state.brushes is brushes and window.state.things is things
    assert window.file_path == str(tmp_path / "maps" / "level.json")
    assert not window.unsaved_changes
    assert "terrain could not be built" in window.toasts[-1][0]


def test_progress_dialogs_are_deleted_when_they_close(main_window, qt_app, monkeypatch):
    from PyQt5.QtWidgets import QProgressDialog
    from editor.terrain_editor import TerrainEditorPanel
    from engine.terrain import Terrain

    panel = TerrainEditorPanel(Terrain(seed=3))
    try:
        for _ in range(3):
            panel.show_progress("Working")
            panel.show_progress("Still working")    # shown again before hidden
            panel.hide_progress()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
        assert panel.findChildren(QProgressDialog) == []
    finally:
        panel.close()
        panel.deleteLater()

    class LightTerrain:
        def __init__(self, *args, **kwargs):
            pass

        def from_dict(self, data):
            pass

        def to_dict(self):
            return {}

        def cleanup(self):
            pass

    import engine.terrain as terrain_module
    monkeypatch.setattr(terrain_module, "Terrain", LightTerrain)
    monkeypatch.setattr(main_window, "_show_terrain_editor_panel", lambda: None)
    main_window.open_terrain_editor()
    qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
    assert main_window.findChildren(QProgressDialog) == []
    main_window.terrain = None
    main_window.state.terrain_data = None
