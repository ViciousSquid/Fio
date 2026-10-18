"""Real ZIP faults must release handles and leave later sound loads usable."""

import struct
import zipfile

import pytest

from engine import audio_archives as audio


def _archive(path, entries, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for name, payload in entries:
            archive.writestr('assets/sounds/' + name, payload)


def _damage_first(path, fault):
    with zipfile.ZipFile(path) as archive:
        info = archive.infolist()[0]
        local = info.header_offset
        central = archive.start_dir
    data = bytearray(path.read_bytes())
    if fault == 'unsupported':
        struct.pack_into('<H', data, local + 8, 99)
        struct.pack_into('<H', data, central + 10, 99)
    elif fault == 'encrypted':
        struct.pack_into('<H', data, local + 6, 1)
        struct.pack_into('<H', data, central + 8, 1)
    else:
        name_size, extra_size = struct.unpack_from('<HH', data, local + 26)
        start = local + 30 + name_size + extra_size
        # Stored data produces a CRC error; reserved DEFLATE block type 3
        # produces a decoder error before a CRC can even be checked.
        if fault == 'lzma':
            data[start + 4] = 255  # Invalid LZMA properties.
        else:
            data[start] = 7 if fault == 'deflate' else data[start] ^ 255
    path.write_bytes(data)


def test_preload_scopes_one_handle_and_sees_replaced_archive(tmp_path, monkeypatch):
    path = tmp_path / 'sounds.fiopak'
    _archive(path, [('b.wav', b'b'), ('a.wav', b'a')])
    opened = []
    original = zipfile.ZipFile

    def track(*args, **kwargs):
        archive = original(*args, **kwargs)
        opened.append(archive)
        return archive

    monkeypatch.setattr(audio.zipfile, 'ZipFile', track)
    with audio.AudioArchiveReader(path, 'assets/sounds') as reader:
        assert reader.members() == ['assets/sounds/a.wav', 'assets/sounds/b.wav']
        assert [reader.read(name) for name in reader.members()] == [b'a', b'b']
        assert reader.read('assets/sounds/missing.wav') is None
        assert reader.read('assets/sounds/../outside.wav') is None
        assert len(opened) == 1
    assert opened[0].fp is None
    replacement = tmp_path / 'new.fiopak'
    with original(replacement, 'w') as archive:
        archive.writestr('assets/sounds/new.wav', b'new')
    replacement.replace(path)
    with audio.AudioArchiveReader(path, 'assets/sounds') as reader:
        assert reader.members() == ['assets/sounds/new.wav']
        assert reader.read('assets/sounds/new.wav') == b'new'
    assert len(opened) == 2
    assert all(archive.fp is None for archive in opened)


def test_eager_size_limit_checks_header_before_decompression(tmp_path, monkeypatch):
    path = tmp_path / 'sounds.fiopak'
    _archive(path, [('at.wav', b'a' * 8), ('over.wav', b'a' * 9)], zipfile.ZIP_DEFLATED)
    monkeypatch.setattr(audio, 'MAX_AUDIO_BYTES', 8)
    opened = []
    original = zipfile.ZipFile.open

    def track(self, member, *args, **kwargs):
        opened.append(member.filename)
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'open', track)
    assert audio.read_archive_audio(path, 'assets/sounds/at.wav', 'assets/sounds') == b'a' * 8
    with pytest.raises(zipfile.BadZipFile, match='size limit'):
        audio.read_archive_audio(path, 'assets/sounds/over.wav', 'assets/sounds')
    assert opened == ['assets/sounds/at.wav']
    # The streaming API keeps its original ownership and size contract.
    archive, stream = audio.open_archive_audio(path, 'assets/sounds/over.wav', 'assets/sounds')
    try:
        assert stream.read() == b'a' * 9
    finally:
        stream.close()
        archive.close()


@pytest.mark.parametrize('fault', ['crc', 'deflate', 'lzma', 'encrypted', 'unsupported'])
def test_bad_member_is_reportable_and_next_member_still_loads(tmp_path, fault):
    path = tmp_path / 'sounds.fiopak'
    if fault == 'lzma':
        pytest.importorskip('lzma')
    compression = {'deflate': zipfile.ZIP_DEFLATED, 'lzma': zipfile.ZIP_LZMA}.get(
        fault, zipfile.ZIP_STORED)
    _archive(path, [('bad.wav', b'some sound bytes' * 20), ('good.wav', b'good')], compression)
    _damage_first(path, fault)
    with audio.AudioArchiveReader(path, 'assets/sounds') as reader:
        with pytest.raises(zipfile.BadZipFile):
            reader.read('assets/sounds/bad.wav')
        assert reader.read('assets/sounds/good.wav') == b'good'
    assert reader.archive.fp is None
    # The single-member wrapper has the same recoverable error contract.
    with pytest.raises(zipfile.BadZipFile):
        audio.read_archive_audio(path, 'assets/sounds/bad.wav', 'assets/sounds')


@pytest.mark.parametrize('fault', ['encrypted', 'unsupported'])
def test_failed_stream_open_releases_owner(tmp_path, monkeypatch, fault):
    path = tmp_path / 'sounds.fiopak'
    _archive(path, [('bad.wav', b'bad')])
    _damage_first(path, fault)
    opened = []
    original = zipfile.ZipFile

    def track(*args, **kwargs):
        archive = original(*args, **kwargs)
        opened.append(archive)
        return archive

    monkeypatch.setattr(audio.zipfile, 'ZipFile', track)
    with pytest.raises(zipfile.BadZipFile):
        audio.open_archive_audio(path, 'assets/sounds/bad.wav', 'assets/sounds')
    assert len(opened) == 1 and opened[0].fp is None


def test_duplicate_names_keep_last_entry_semantics(tmp_path):
    path = tmp_path / 'sounds.fiopak'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('assets/sounds/a.wav', b'first')
        with pytest.warns(UserWarning, match='Duplicate name'):
            archive.writestr('assets/sounds/a.wav', b'last')
    with audio.AudioArchiveReader(path, 'assets/sounds') as reader:
        assert reader.members() == ['assets/sounds/a.wav']
        assert reader.read('assets/sounds/a.wav') == b'last'


def test_reader_closes_if_consumer_aborts(tmp_path):
    path = tmp_path / 'sounds.fiopak'
    _archive(path, [('a.wav', b'a')])
    with pytest.raises(LookupError):
        with audio.AudioArchiveReader(path, 'assets/sounds') as reader:
            assert reader.read('assets/sounds/a.wav') == b'a'
            raise LookupError('decoder failed')
    assert reader.archive.fp is None
