"""An archived asset is read without parsing its archive again, and the
archive is still never held open.

Regression covered: every read of an archived asset (a category archive, or
a played package's member) opened a ``ZipFile``, which reads and parses the
whole central directory -- 12 ms a read at 5000 members, before the member's
own few bytes. The index each archive already has locates the member; only
its bytes are read, from a file opened for that read alone, so a repack can
still replace the archive (which Windows refuses while a handle is open).
"""

import os
import zipfile

import pytest

from engine import asset_archives
from engine.asset_archives import (asset_bytes, prepare_asset_archives,
                                   read_zip_entry, read_zip_member)


@pytest.fixture(autouse=True)
def _forget_the_index():
    saved = dict(asset_archives._state)
    yield
    asset_archives._state.update(saved)
    asset_archives._zip_indexes.clear()


def _archive(path, members, compression=zipfile.ZIP_DEFLATED):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


@pytest.fixture
def zip_opens(monkeypatch):
    """How many times the archive code constructs a ZipFile."""
    count = [0]
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, *args, **kwargs):
            count[0] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(asset_archives.zipfile, "ZipFile", Counting)
    return count


def _members(count):
    return {f"assets/textures/t{i}.png": (b"texture %d " % i) * (i % 7 + 1)
            for i in range(count)}


def test_reads_parse_the_archive_once_not_once_a_read(tmp_path, monkeypatch, zip_opens):
    monkeypatch.chdir(tmp_path)
    members = _members(300)
    _archive(tmp_path / "assets" / "textures.fiopak", members)
    prepare_asset_archives(tmp_path)
    indexed = zip_opens[0]
    for name, data in members.items():
        assert asset_bytes(name) == data
    assert zip_opens[0] == indexed               # the index was enough


def test_a_played_package_member_is_read_by_its_index(tmp_path, monkeypatch, zip_opens):
    package = _archive(tmp_path / "game.fiopak", _members(200))
    zip_opens[0] = 0
    for name, data in _members(200).items():
        assert read_zip_entry(package, name) == data
    assert zip_opens[0] == 1
    with pytest.raises(KeyError):
        read_zip_entry(package, "assets/textures/missing.png")


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                         zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_member_bytes_match_zipfile_for_every_method(tmp_path, compression):
    members = {"a.bin": os.urandom(4096), "b.txt": b"repeat " * 2000, "empty": b""}
    path = _archive(tmp_path / "m.zip", members, compression)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            assert read_zip_member(path, info) == archive.read(info)


def test_a_corrupted_member_is_refused_not_returned(tmp_path):
    path = _archive(tmp_path / "m.zip", {"a.txt": b"hello world " * 100},
                    zipfile.ZIP_STORED)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("a.txt")
    raw = bytearray(path.read_bytes())
    data_at = raw.index(b"hello world")
    raw[data_at] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(zipfile.BadZipFile):
        read_zip_member(path, info)


def test_a_repack_is_seen_and_the_archive_is_not_held(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archive = _archive(tmp_path / "assets" / "textures.fiopak",
                       {"assets/textures/a.png": b"first"})
    prepare_asset_archives(tmp_path)
    assert asset_bytes("assets/textures/a.png") == b"first"
    if os.path.isdir("/proc/self/fd"):
        held = {os.path.realpath(os.path.join("/proc/self/fd", fd))
                for fd in os.listdir("/proc/self/fd")}
        assert str(archive.resolve()) not in held
    # Replaced the way tools/pack_assets.py does it: a new file moved over it.
    replacement = _archive(tmp_path / "new.fiopak",
                           {"assets/textures/b.png": b"padding" * 10,
                            "assets/textures/a.png": b"second version"})
    os.replace(replacement, archive)
    assert asset_bytes("assets/textures/a.png") == b"second version"


def test_a_replaced_package_is_indexed_again(tmp_path):
    package = _archive(tmp_path / "game.fiopak", {"maps/a.json": b"{}"})
    assert read_zip_entry(package, "maps/a.json") == b"{}"
    replacement = _archive(tmp_path / "next.fiopak",
                           {"assets/x.png": b"x" * 50, "maps/a.json": b'{"v": 2}'})
    os.replace(replacement, package)
    assert read_zip_entry(package, "maps/a.json") == b'{"v": 2}'


def test_package_session_reads_through_the_index(tmp_path, monkeypatch):
    pytest.importorskip("PyQt5")
    import json
    from editor.package_session import PackageSession
    package = _archive(tmp_path / "game.fiopak", {
        "metadata.json": json.dumps({"map_path": "maps/start.json"}).encode(),
        "maps/start.json": json.dumps({"brushes": [], "things": []}).encode(),
        "assets/textures/a.png": b"tex"})
    session = PackageSession(str(package))
    opened = [0]
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, *args, **kwargs):
            opened[0] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", Counting)     # the one module both use
    for _ in range(20):
        assert session.read("assets/textures/a.png") == b"tex"
        assert session.load_json("maps/start.json") == {"brushes": [], "things": []}
    assert opened[0] <= 1
