"""A save that fails must leave the previous file exactly as it was.

Every writer here used to ``open(path, "w")`` and then produce the document:
the truncation happened first, so an entity that would not serialise (or a
full disk) turned the user's map, autosave or quicksave slot into an empty
file.
"""

import json

import pytest

from engine import savegame
from engine.fileio import write_json_atomic


class _Unserialisable:
    """Defeats even savegame's permissive JSON fallback."""

    def tolist(self):
        raise RuntimeError("no list form")

    def __iter__(self):
        raise RuntimeError("cannot iterate")


def test_atomic_write_round_trips_and_leaves_no_temporary(tmp_path):
    target = tmp_path / "doc.json"
    write_json_atomic(str(target), {"a": [1, 2]}, indent=2)
    assert json.loads(target.read_text()) == {"a": [1, 2]}
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]


def test_atomic_write_failure_keeps_the_old_document(tmp_path):
    target = tmp_path / "doc.json"
    target.write_text('{"old": true}')
    with pytest.raises(TypeError):
        write_json_atomic(str(target), {"bad": object()})
    assert target.read_text() == '{"old": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]


def test_a_failed_quicksave_keeps_the_previous_save(tmp_path):
    slot = tmp_path / "saves" / "quicksave.fiosave"
    savegame.write(str(slot), {"fio_savegame": True, "save_version": 2, "n": 1})
    before = slot.read_bytes()

    with pytest.raises(RuntimeError):
        savegame.write(str(slot), {"fio_savegame": True, "save_version": 2,
                                   "n": _Unserialisable()})

    assert slot.read_bytes() == before
    assert savegame.read(str(slot))["n"] == 1


@pytest.mark.qt
def test_a_failed_level_save_keeps_the_map(tmp_path):
    pytest.importorskip("PyQt5", reason="save_level lives on the editor window")
    from editor.main_window import MainWindow

    level = tmp_path / "level.json"
    level.write_text(json.dumps({"version": 3, "brushes": [{"id": "keep"}],
                                 "things": []}))
    before = level.read_bytes()
    toasts = []

    class _Window:
        file_path = str(level)
        unsaved_changes = True

        class state:
            @staticmethod
            def get_level_data():
                raise RuntimeError("an entity would not serialise")

        def stop_mover_preview(self):
            pass

        def show_toast(self, message, is_error=False, **_kw):
            toasts.append((message, is_error))

    window = _Window()
    MainWindow.save_level(window)

    assert level.read_bytes() == before, "a failed save emptied the map"
    assert window.unsaved_changes, "a failed save was reported as saved"
    assert toasts and toasts[-1][1]


@pytest.mark.parametrize("version", ["2", None, [2], True])
def test_a_save_with_a_malformed_version_is_refused_clearly(tmp_path, version):
    path = tmp_path / "odd.fiosave"
    path.write_text(json.dumps({"fio_savegame": True, "save_version": version}))
    with pytest.raises(ValueError, match="invalid save_version"):
        savegame.read(str(path))


def test_a_save_from_a_newer_build_is_refused(tmp_path):
    path = tmp_path / "future.fiosave"
    path.write_text(json.dumps({"fio_savegame": True,
                                "save_version": savegame.SAVE_VERSION + 1}))
    with pytest.raises(ValueError, match="newer than this build"):
        savegame.read(str(path))
