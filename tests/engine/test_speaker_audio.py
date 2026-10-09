"""Speakers play only from assets/sounds and assets/music."""

import os

import pytest

from engine.speaker_audio import list_speaker_audio, resolve_speaker_sound, speaker_sound_path


@pytest.mark.parametrize("value, expected", [
    ("assets/sounds/beep.wav", "assets/sounds/beep.wav"),
    ("assets/music/theme.ogg", "assets/music/theme.ogg"),
    ("assets/music/act1/theme.mp3", "assets/music/act1/theme.mp3"),
    ("music/theme.ogg", "assets/music/theme.ogg"),
    ("sounds\\ui\\click.wav", "assets/sounds/ui/click.wav"),
    ("beep.wav", "assets/sounds/beep.wav"),          # older maps: a bare name
    ("./assets/sounds/beep.wav", "assets/sounds/beep.wav"),
])
def test_allowed(value, expected):
    assert speaker_sound_path(value) == expected


@pytest.mark.parametrize("value", [
    "", None, "assets/textures/beep.wav", "assets/sprites/x.ogg", "maps/x.wav",
    "assets/sounds/../textures/x.wav", "../secret.wav", "/etc/x.wav",
    "C:/Windows/x.wav", "assets/sounds/readme.txt", "assets/sounds", "assets/music/",
])
def test_refused(value):
    assert speaker_sound_path(value) is None


def test_resolving_refuses_a_symlink_out_of_the_folder(tmp_path):
    (tmp_path / "assets" / "music").mkdir(parents=True)
    (tmp_path / "outside.ogg").write_bytes(b"")
    inside = tmp_path / "assets" / "music" / "theme.ogg"
    inside.write_bytes(b"")
    assert resolve_speaker_sound("assets/music/theme.ogg", str(tmp_path)) == os.path.realpath(inside)
    try:
        os.symlink(tmp_path / "outside.ogg", tmp_path / "assets" / "music" / "link.ogg")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert resolve_speaker_sound("assets/music/link.ogg", str(tmp_path)) is None


def test_listing_walks_subfolders_and_skips_other_files(tmp_path):
    music = tmp_path / "assets" / "music"
    (music / "act1").mkdir(parents=True)
    for name in ("b.ogg", "act1/a.mp3", "notes.txt"):
        (music / name).write_bytes(b"")
    assert list_speaker_audio(str(tmp_path), "music") == [
        "assets/music/act1/a.mp3", "assets/music/b.ogg"]
    assert list_speaker_audio(str(tmp_path), "textures") == []
