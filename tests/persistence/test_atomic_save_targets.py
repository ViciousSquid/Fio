"""An atomic save writes through a symlink, keeps the file's permissions,
syncs the directory, and refuses NaN before the old save is replaced.

Regressions covered:

* saving through a symlink replaced the link with a regular file, and the
  link's target -- the real document -- kept its old contents;
* the replaced file's permission bits were reset to ``0666 & ~umask``;
* the rename was never flushed to the directory (POSIX), so a power cut
  just after a save could bring the old file back;
* ``savegame.write`` used ``json.dumps``' default ``allow_nan=True``: a NaN
  coordinate saved "fine" over the good save, and then the save was refused
  on load by ``validate_snapshot``.
"""

import json
import math
import os
import stat

import pytest

from engine import fileio, savegame

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX links and modes")


@posix_only
def test_a_save_through_a_symlink_updates_its_target(tmp_path):
    real_dir = tmp_path / "synced"
    real_dir.mkdir()
    target = real_dir / "map.json"
    target.write_text('{"old": true}')
    link = tmp_path / "map.json"
    link.symlink_to(target)

    fileio.write_json_atomic(link, {"new": True})

    assert link.is_symlink()
    assert json.loads(target.read_text()) == {"new": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["map.json", "synced"]
    assert sorted(p.name for p in real_dir.iterdir()) == ["map.json"]


@posix_only
def test_a_save_through_a_dangling_symlink_creates_its_target(tmp_path):
    target = tmp_path / "real.json"
    link = tmp_path / "map.json"
    link.symlink_to(target)
    fileio.write_text_atomic(link, "made")
    assert link.is_symlink() and target.read_text() == "made"


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o640, 0o664, 0o444])
def test_a_replaced_file_keeps_its_permissions(tmp_path, mode):
    target = tmp_path / "save.fiosave"
    target.write_text("old")
    os.chmod(target, mode)
    fileio.write_text_atomic(target, "new")
    assert target.read_text() == "new"
    assert stat.S_IMODE(os.stat(target).st_mode) == mode


@posix_only
def test_a_new_file_gets_ordinary_permissions(tmp_path):
    umask = os.umask(0o022)
    try:
        fileio.write_text_atomic(tmp_path / "fresh.json", "x")
    finally:
        os.umask(umask)
    assert stat.S_IMODE(os.stat(tmp_path / "fresh.json").st_mode) == 0o644


@posix_only
def test_the_directory_is_synced_after_the_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(descriptor):
        events.append(("fsync", stat.S_ISDIR(os.fstat(descriptor).st_mode)))
        return real_fsync(descriptor)

    def replace(src, dst):
        events.append(("replace", None))
        return real_replace(src, dst)

    monkeypatch.setattr(fileio.os, "fsync", fsync)
    monkeypatch.setattr(fileio.os, "replace", replace)
    fileio.write_text_atomic(tmp_path / "doc.json", "x")
    assert events == [("fsync", False), ("replace", None), ("fsync", True)]


def test_a_failing_directory_sync_does_not_fail_the_save(tmp_path, monkeypatch):
    real_fsync = os.fsync

    def fsync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("not supported here")
        return real_fsync(descriptor)

    monkeypatch.setattr(fileio.os, "fsync", fsync)
    fileio.write_text_atomic(tmp_path / "doc.json", "saved")
    assert (tmp_path / "doc.json").read_text() == "saved"


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_save_with_nan_fails_and_keeps_the_previous_save(tmp_path, bad):
    path = tmp_path / "slot1.fiosave"
    good = {"fio_savegame": True, "save_version": savegame.SAVE_VERSION,
            "player": {"pos": [1.0, 2.0, 3.0]}}
    savegame.write(str(path), good)
    before = path.read_bytes()

    broken = dict(good, player={"pos": [bad, 2.0, 3.0]})
    with pytest.raises(ValueError):
        savegame.write(str(path), broken)

    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["slot1.fiosave"]


def test_a_numpy_nan_is_refused_too(tmp_path):
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError):
        savegame.write(str(tmp_path / "s.fiosave"),
                       {"fio_savegame": True, "pos": np.array([np.nan, 0.0, 0.0])})
    assert not (tmp_path / "s.fiosave").exists()
