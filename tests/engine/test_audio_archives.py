"""Tests for audio discovery and reading from .fiopak ZIP archives."""

import zipfile

from engine.audio_archives import list_archive_audio, open_archive_audio, read_archive_audio


def _write_archive(path, members):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_lists_only_safe_audio_below_requested_root(tmp_path):
    path = tmp_path / "sounds.fiopak"
    _write_archive(path, {
        "assets/sounds/z.wav": b"wav",
        "assets/sounds/ui/click.ogg": b"ogg",
        "assets/sounds/readme.txt": b"text",
        "assets/music/theme.mp3": b"mp3",
        "assets/sounds/../outside.wav": b"unsafe",
        "/assets/sounds/absolute.wav": b"unsafe",
    })
    assert list_archive_audio(str(path), "assets/sounds") == [
        "assets/sounds/ui/click.ogg", "assets/sounds/z.wav",
    ]


def test_reads_requested_audio_without_extracting(tmp_path):
    path = tmp_path / "sounds.fiopak"
    _write_archive(path, {"assets/sounds/shoot.wav": b"wave-data"})
    assert read_archive_audio(str(path), "assets/sounds/shoot.wav", "assets/sounds") == b"wave-data"
    assert read_archive_audio(str(path), "assets/music/shoot.wav", "assets/sounds") is None
    assert read_archive_audio(str(path), "assets/sounds/../secret.wav", "assets/sounds") is None


def test_opened_member_keeps_zip_available_until_closed(tmp_path):
    path = tmp_path / "music.fiopak"
    _write_archive(path, {"assets/music/theme.mp3": b"music-data"})
    opened = open_archive_audio(path, "assets/music/theme.mp3", "assets/music", (".mp3",))
    assert opened is not None
    archive, stream = opened
    try:
        assert stream.read() == b"music-data"
        assert not archive.fp.closed
    finally:
        stream.close()
        archive.close()
    assert archive.fp is None


def test_rejects_wrong_extension(tmp_path):
    path = tmp_path / "music.fiopak"
    _write_archive(path, {"assets/music/readme.txt": b"text"})
    assert list_archive_audio(str(path), "assets/music", (".mp3",)) == []
