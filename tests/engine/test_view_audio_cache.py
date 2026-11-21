"""The 3D view's sound cache: misses remembered, levels' sounds loaded up
front and let go after.

A missing sound was looked for again -- the .fiopak's directory rescanned on
the UI thread -- every time anything asked for it. A level's music was decoded
whole by the frame that first played it. And ``sound_pool`` kept every sound
every level had played for the life of the editor.

Counts lookups against stand-ins for the decoder; no audio device is used.
"""

from types import SimpleNamespace

import pytest

pygame = pytest.importorskip("pygame")

from engine.view_audio import ViewAudio  # noqa: E402

pytestmark = pytest.mark.perf


@pytest.fixture
def audio(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets" / "sounds").mkdir(parents=True)
    (tmp_path / "assets" / "music").mkdir(parents=True)
    audio = ViewAudio(SimpleNamespace())
    monkeypatch.setattr(audio, "_ensure_pygame_mixer", lambda: True)
    audio.loads = []

    def load(name, path):
        audio.loads.append(name)
        audio.sound_pool[name] = object()
        return True

    monkeypatch.setattr(audio, "_load_sound_to_cache", load)
    audio.pack_lookups = []

    def pack(names, path):
        audio.pack_lookups.append(names[0])
        return False

    monkeypatch.setattr(audio, "_load_user_pack_sound", pack)
    return audio


def _speaker(sound_file):
    return SimpleNamespace(properties={"sound_file": sound_file})


def test_a_missing_sound_is_looked_for_once(audio):
    for _ in range(50):
        assert audio._get_sound_instance("nope.wav") is None
        assert audio._get_sound_instance("assets/music/gone.ogg") is None
    assert audio.pack_lookups == ["nope.wav", "assets/music/gone.ogg"]


def test_a_miss_is_forgotten_when_the_next_level_starts(audio, tmp_path):
    assert audio._get_sound_instance("late.wav") is None
    (tmp_path / "assets" / "sounds" / "late.wav").write_bytes(b"RIFF")
    assert audio._get_sound_instance("late.wav") is None     # still remembered
    audio.begin_level([])
    assert audio._get_sound_instance("late.wav") is not None


def test_nothing_is_remembered_while_the_mixer_is_down(audio, monkeypatch):
    monkeypatch.setattr(audio, "_ensure_pygame_mixer", lambda: False)
    audio._get_sound_instance("nope.wav")
    assert audio._missing_sounds == set()


def test_a_level_loads_its_speaker_sounds_and_music_as_it_starts(audio, tmp_path):
    (tmp_path / "assets" / "music" / "theme.ogg").write_bytes(b"OggS")
    (tmp_path / "assets" / "sounds" / "hum.wav").write_bytes(b"RIFF")

    loaded = audio.begin_level([_speaker("assets/music/theme.ogg"), _speaker("hum.wav"),
                                _speaker(""), SimpleNamespace(properties=None)])

    assert loaded == 2
    assert sorted(audio.loads) == ["assets/music/theme.ogg", "hum.wav"]
    audio.loads.clear()
    audio._get_sound_instance("assets/music/theme.ogg")      # first play
    assert audio.loads == [], "the first play decoded the music again"


def test_starting_a_level_lets_go_of_the_last_levels_sounds(audio, tmp_path):
    audio.sound_pool["shot.wav"] = "core"
    audio._core_sound_keys = frozenset({"shot.wav"})
    (tmp_path / "assets" / "music" / "one.ogg").write_bytes(b"OggS")
    (tmp_path / "assets" / "music" / "two.ogg").write_bytes(b"OggS")

    audio.begin_level([_speaker("assets/music/one.ogg")])
    assert set(audio.sound_pool) == {"shot.wav", "assets/music/one.ogg"}

    audio.begin_level([_speaker("assets/music/two.ogg")])
    assert set(audio.sound_pool) == {"shot.wav", "assets/music/two.ogg"}, (
        "the previous level's music stayed loaded")
    assert audio.sound_pool["shot.wav"] == "core"


def test_a_sound_both_levels_use_is_not_decoded_again(audio, tmp_path):
    (tmp_path / "assets" / "music" / "theme.ogg").write_bytes(b"OggS")
    audio.begin_level([_speaker("assets/music/theme.ogg")])
    audio.begin_level([_speaker("music/theme.ogg")])
    assert audio.loads == ["assets/music/theme.ogg"]
