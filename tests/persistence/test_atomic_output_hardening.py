"""A save owns its temporary file; old siblings and other saves survive."""

import json
import os
import stat
import zipfile
from types import SimpleNamespace

import pytest

from editor.package_exporter import PackageExporter
from engine import fileio, terrain_stamp


def _writer(tmp_path, kind):
    target = tmp_path / {"text": "map.json", "stamp": "paint.stamp",
                         "package": "world.fiopak"}[kind]
    if kind == "text":
        def write():
            fileio.write_json_atomic(target, {"new": "caf\u00e9"})
    elif kind == "stamp":
        terrain = SimpleNamespace(paint_stamps=[])

        def write():
            return terrain_stamp.save_stamp_file(terrain, target)
    else:
        project = tmp_path / "project"
        (project / "maps").mkdir(parents=True)
        level = project / "maps" / "start.json"
        level.write_text(json.dumps({"brushes": [], "things": []}))
        exporter = PackageExporter(None, str(project))

        def write():
            success, errors = exporter.export(target, {}, str(level))
            if not success:
                raise OSError(errors[-1])
    return target, write


def _old_temporary(target, kind, sibling_kind):
    name = f".{target.name}.tmp" if kind == "package" else f"{target.name}.tmp"
    sibling = target.with_name(name)
    if sibling_kind == "regular":
        sibling.write_bytes(b"unrelated temporary document")
        return sibling, sibling
    other = target.with_name("unrelated-document.txt")
    other.write_bytes(b"unrelated temporary document")
    sibling.symlink_to(other)
    return sibling, other


def _assert_sibling_untouched(sibling, other, sibling_kind):
    assert sibling.exists()
    assert sibling.is_symlink() == (sibling_kind == "symlink")
    assert other.read_bytes() == b"unrelated temporary document"


@pytest.mark.parametrize("kind", ["text", "stamp", "package"])
@pytest.mark.parametrize("sibling_kind", ["regular", "symlink"])
def test_existing_temporary_is_not_overwritten_on_success(tmp_path, kind, sibling_kind):
    target, write = _writer(tmp_path, kind)
    sibling, other = _old_temporary(target, kind, sibling_kind)
    before_names = set(tmp_path.iterdir())

    result = write()

    _assert_sibling_untouched(sibling, other, sibling_kind)
    assert set(tmp_path.iterdir()) == before_names | {target}
    if kind == "package":
        with zipfile.ZipFile(target) as package:
            assert json.loads(package.read("metadata.json"))["map_path"] == "maps/start.json"
            assert "maps/start.json" in package.namelist()
    elif kind == "stamp":
        assert result == json.loads(target.read_text())
        assert result["stamps"] == []
    else:
        assert json.loads(target.read_text()) == {"new": "caf\u00e9"}


class _FailingWriter:
    def __init__(self, handle):
        self.handle = handle
        self.failed = False

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def write(self, data):
        if not self.failed:
            self.failed = True
            self.handle.write(data[:1])
            raise OSError("injected partial write failure")
        return self.handle.write(data)


@pytest.mark.parametrize("kind", ["text", "stamp", "package"])
@pytest.mark.parametrize("sibling_kind", ["regular", "symlink"])
@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_failed_save_keeps_target_and_existing_temporary(
        tmp_path, monkeypatch, kind, sibling_kind, failure):
    target, write = _writer(tmp_path, kind)
    target.write_bytes(b"the previous saved document")
    sibling, other = _old_temporary(target, kind, sibling_kind)
    before_names = set(tmp_path.iterdir())

    def fail(*_args, **_kwargs):
        raise OSError(f"injected {failure} failure")

    if failure == "write":
        real_fdopen = os.fdopen
        monkeypatch.setattr(fileio.os, "fdopen", lambda *a, **kw: _FailingWriter(real_fdopen(*a, **kw)))
    else:
        monkeypatch.setattr(fileio.os, failure, fail)

    with pytest.raises(OSError, match="injected"):
        write()

    assert target.read_bytes() == b"the previous saved document"
    _assert_sibling_untouched(sibling, other, sibling_kind)
    assert set(tmp_path.iterdir()) == before_names


def test_overlapping_saves_use_independent_files(tmp_path):
    target = tmp_path / "document.json"
    with fileio.atomic_output(target) as outer:
        outer.write("outer document")
        fileio.write_text_atomic(target, "inner document")
        assert target.read_text() == "inner document"
    assert target.read_text() == "outer document"
    assert list(tmp_path.iterdir()) == [target]


def test_exclusive_creation_retries_without_following_a_symlink(tmp_path, monkeypatch):
    target = tmp_path / "document.json"
    other = tmp_path / "unrelated.txt"
    other.write_text("keep")
    collision = tmp_path / ".fio-collision.tmp"
    collision.symlink_to(other)
    tokens = iter(("collision", "unused"))
    monkeypatch.setattr(fileio.secrets, "token_hex", lambda _size: next(tokens))

    fileio.write_text_atomic(target, "new document")

    assert other.read_text() == "keep"
    assert collision.is_symlink()
    assert target.read_text() == "new document"
    assert set(tmp_path.iterdir()) == {target, collision, other}


def test_temporary_creation_failure_leaves_existing_sibling_alone(tmp_path, monkeypatch):
    target = tmp_path / "document.json"
    target.write_text("old")
    collision = tmp_path / ".fio-collision.tmp"
    collision.write_text("keep")
    monkeypatch.setattr(fileio.secrets, "token_hex", lambda _size: "collision")

    with pytest.raises(FileExistsError, match="exclusive"):
        fileio.write_text_atomic(target, "new")

    assert target.read_text() == "old"
    assert collision.read_text() == "keep"
    assert set(tmp_path.iterdir()) == {target, collision}


def test_fdopen_failure_closes_descriptor_and_removes_owned_temporary(tmp_path, monkeypatch):
    target = tmp_path / "document.json"
    descriptors = []

    def fail(descriptor, *_args, **_kwargs):
        descriptors.append(descriptor)
        raise OSError("fdopen failure")

    monkeypatch.setattr(fileio.os, "fdopen", fail)
    with pytest.raises(OSError, match="fdopen"):
        fileio.write_text_atomic(target, "new")

    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert list(tmp_path.iterdir()) == []


def test_new_document_permissions_match_an_ordinary_open(tmp_path):
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary")
    target = tmp_path / "atomic.txt"

    fileio.write_text_atomic(target, "atomic")

    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(ordinary.stat().st_mode)


def test_text_encoding_and_relative_path_are_preserved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fileio.write_text_atomic("document.txt", "caf\u00e9", encoding="latin-1")
    assert (tmp_path / "document.txt").read_bytes() == b"caf\xe9"


def test_serialization_failure_never_creates_a_stamp_temporary(tmp_path):
    target = tmp_path / "paint.stamp"
    target.write_text("previous stamp")
    terrain = SimpleNamespace(paint_stamps=[{key: object() for key in terrain_stamp._STAMP_FIELDS}])

    with pytest.raises(TypeError):
        terrain_stamp.save_stamp_file(terrain, target)

    assert target.read_text() == "previous stamp"
    assert list(tmp_path.iterdir()) == [target]
