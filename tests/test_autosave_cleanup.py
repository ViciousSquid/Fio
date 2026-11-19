"""Tests leave no editor autosaves behind (conftest._no_autosaves_left_behind),
and a stray autosave is never taken for a shipped map."""

import json
import os

import conftest
from tests.helpers import brush_semantics as bs


def _write(path, text="{}"):
    with open(path, "w") as handle:
        handle.write(text)


def test_an_autosave_written_during_a_test_is_removed(tmp_path):
    kept = tmp_path / "mine_autosave.json"
    _write(kept)
    stat = os.stat(kept)
    before = {str(kept): (stat.st_mtime_ns, stat.st_size)}
    new = tmp_path / "untitled_autosave.json"
    _write(new)
    found = dict(before)
    stat = os.stat(new)
    found[str(new)] = (stat.st_mtime_ns, stat.st_size)
    assert conftest.remove_new_autosaves(before, found) == [str(new)]
    assert not new.exists() and kept.exists()     # one already there: left alone


def test_an_autosave_rewritten_during_a_test_is_removed(tmp_path):
    path = tmp_path / "level_autosave.json"
    _write(path)
    before = {str(path): (1, 2)}                   # it was different before
    stat = os.stat(path)
    assert conftest.remove_new_autosaves(
        before, {str(path): (stat.st_mtime_ns, stat.st_size)}) == [str(path)]
    assert not path.exists()


def test_the_checkout_holds_no_autosave_written_by_an_earlier_test():
    """The fixture runs after every test: by now only autosaves that were in
    maps/ before the run began can remain, and none was written since."""
    for path, (mtime, _size) in conftest._autosaves().items():
        assert mtime < conftest.SESSION_STARTED_NS, path


def test_an_autosave_is_not_a_shipped_map(tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    level = {"version": 5, "brushes": [{"id": "a", "pos": [0, 0, 0], "size": [64, 64, 64]}]}
    _write(maps / "Level.json", json.dumps(level))
    _write(maps / "untitled_autosave.json", json.dumps(level))
    assert sorted(bs.shipped_maps(str(tmp_path))) == ["Level.json"]
