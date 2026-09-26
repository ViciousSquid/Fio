"""Behaviour-preservation tests for the mover/door scalar maths, the looping
speaker queue protocol, and the floating-window manager."""

import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.helpers.paths import read_source  # noqa: E402


# ---------------------------------------------------------------------------
# Mover / door offset maths
# ---------------------------------------------------------------------------

def _reference_offset(original, direction, distance, factor, current):
    """The NumPy expression the scalar maths replaced."""
    original = np.array(original)
    direction = np.array(direction, dtype=float)
    offset = direction * distance * factor
    new_pos = original + offset
    move_delta = new_pos - np.array(current)
    return new_pos.tolist(), move_delta


def _scalar_offset(original, direction, distance, factor, current):
    """The scalar form now in logic_thread (same association order)."""
    nx = original[0] + (direction[0] * distance) * factor
    ny = original[1] + (direction[1] * distance) * factor
    nz = original[2] + (direction[2] * distance) * factor
    return [nx, ny, nz], (nx - current[0], ny - current[1], nz - current[2])


def test_scalar_offset_is_bit_identical_to_numpy():
    rng = random.Random(101)
    for _ in range(2000):
        original = [rng.uniform(-4000, 4000) for _ in range(3)]
        current = [rng.uniform(-4000, 4000) for _ in range(3)]
        # Unit direction, as _init_doors / the mover init guarantee.
        d = np.array([rng.uniform(-1, 1) for _ in range(3)], dtype=float)
        n = np.linalg.norm(d)
        if n == 0:
            continue
        d = d / n
        direction = (float(d[0]), float(d[1]), float(d[2]))
        distance = rng.uniform(1, 1000)
        factor = rng.uniform(0.0, 1.0)

        ref_pos, ref_delta = _reference_offset(
            original, direction, distance, factor, current)
        got_pos, got_delta = _scalar_offset(
            original, direction, distance, factor, current)

        assert got_pos == ref_pos
        assert tuple(got_delta) == tuple(float(v) for v in ref_delta)


def test_scalar_offset_yields_plain_python_floats():
    """brush['pos'] must stay JSON-serialisable, not hold numpy scalars."""
    pos, _ = _scalar_offset([0.0, 0.0, 0.0], (0.0, 1.0, 0.0), 128.0, 0.5,
                            [0.0, 0.0, 0.0])
    for v in pos:
        assert type(v) is float, f"expected float, got {type(v)}"
    import json
    json.dumps({"pos": pos})  # must not raise


def test_direction_cache_in_logic_thread_is_a_float_tuple():
    """Guard the regression: a numpy _direction_np would poison brush['pos']."""
    src = read_source("engine", "logic_thread.py")
    assert "'_direction_np': (float(direction[0])" in src
    assert "direction = (float(d[0]), float(d[1]), float(d[2]))" in src
    # The old allocating form must be gone from the two mover/door sites.
    assert "offset = direction * distance" not in src
    assert "np.array(brush['original_pos'])" not in src


def test_endpoints_are_exact():
    """Fully closed and fully open must land exactly on the endpoints."""
    original = [10.0, 20.0, 30.0]
    direction = (0.0, 1.0, 0.0)
    distance = 128.0
    closed, _ = _scalar_offset(original, direction, distance, 0.0, original)
    assert closed == original
    opened, _ = _scalar_offset(original, direction, distance, 1.0, original)
    assert opened == [10.0, 148.0, 30.0]


# ---------------------------------------------------------------------------
# Speaker queue protocol
# ---------------------------------------------------------------------------

class _FakeChannel:
    def __init__(self):
        self.stopped = False
        self.volume = None

    def stop(self):
        self.stopped = True

    def set_volume(self, v):
        self.volume = v


class _FakeSound:
    def __init__(self):
        self.plays = []

    def play(self, loops=0):
        ch = _FakeChannel()
        self.plays.append((loops, ch))
        return ch


class _FakeGameState:
    def __init__(self, requests):
        self._requests = requests

    def consume_sounds(self):
        out, self._requests = self._requests, []
        return out


class _SpeakerHost:
    """Minimal stand-in exercising the exact _process_sound_queue logic."""

    def __init__(self, requests, sound):
        self.game_state = _FakeGameState(requests)
        self._sound = sound

    def _get_sound_instance(self, name):
        return self._sound

    # Mirrors engine.qt_game_view.QtGameView._process_sound_queue.
    def _process_sound_queue(self):
        speaker_channels = getattr(self, "_speaker_channels", None)
        if speaker_channels is None:
            speaker_channels = self._speaker_channels = {}
        for request in self.game_state.consume_sounds():
            action = request.get('action', 'play')
            entity_id = request.get('entity_id')
            if action == 'stop':
                channel = speaker_channels.pop(entity_id, None)
                if channel is not None:
                    channel.stop()
                continue
            sound_file = request.get('file')
            volume = request.get('volume', 1.0)
            if not sound_file:
                continue
            sound = self._get_sound_instance(sound_file)
            if not sound:
                continue
            loops = -1 if request.get('looping') else 0
            if entity_id is not None:
                prev = speaker_channels.pop(entity_id, None)
                if prev is not None:
                    prev.stop()
            channel = sound.play(loops=loops)
            if channel:
                channel.set_volume(volume)
                if entity_id is not None and loops != 0:
                    speaker_channels[entity_id] = channel


def test_looping_speaker_plays_with_infinite_loops():
    snd = _FakeSound()
    host = _SpeakerHost(
        [{'action': 'play', 'file': 'hum.wav', 'volume': 0.5,
          'looping': True, 'entity_id': 1}], snd)
    host._process_sound_queue()
    assert snd.plays[0][0] == -1
    assert snd.plays[0][1].volume == 0.5
    assert 1 in host._speaker_channels


def test_non_looping_speaker_plays_once_and_is_not_tracked():
    snd = _FakeSound()
    host = _SpeakerHost(
        [{'action': 'play', 'file': 'ding.wav', 'looping': False,
          'entity_id': 2}], snd)
    host._process_sound_queue()
    assert snd.plays[0][0] == 0
    assert 2 not in host._speaker_channels


def test_stop_sound_silences_a_looping_channel():
    snd = _FakeSound()
    host = _SpeakerHost(
        [{'action': 'play', 'file': 'hum.wav', 'looping': True, 'entity_id': 3}],
        snd)
    host._process_sound_queue()
    channel = host._speaker_channels[3]
    host.game_state._requests = [{'action': 'stop', 'entity_id': 3}]
    host._process_sound_queue()
    assert channel.stopped
    assert 3 not in host._speaker_channels


def test_retriggering_a_looping_speaker_does_not_stack():
    snd = _FakeSound()
    host = _SpeakerHost(
        [{'action': 'play', 'file': 'hum.wav', 'looping': True, 'entity_id': 4}],
        snd)
    host._process_sound_queue()
    first = host._speaker_channels[4]
    host.game_state._requests = [
        {'action': 'play', 'file': 'hum.wav', 'looping': True, 'entity_id': 4}]
    host._process_sound_queue()
    assert first.stopped, "the previous looping channel must be stopped first"
    assert host._speaker_channels[4] is not first


def test_stop_for_an_unknown_entity_is_harmless():
    host = _SpeakerHost([{'action': 'stop', 'entity_id': 999}], _FakeSound())
    host._process_sound_queue()  # must not raise


def test_legacy_request_without_action_still_plays():
    """Existing callers queue a bare {'file','volume'} dict."""
    snd = _FakeSound()
    host = _SpeakerHost([{'file': 'splash.wav', 'volume': 0.8}], snd)
    host._process_sound_queue()
    assert snd.plays[0][0] == 0
    assert snd.plays[0][1].volume == 0.8


def test_speaker_handlers_queue_spatial_radius_data():
    src = read_source("editor", "io_handlers.py")
    assert "'position': position," in src
    assert "'radius': radius," in src
    assert "'global': global_sound," in src


def test_speaker_start_on_is_consumed_at_player_spawn():
    src = read_source("engine", "logic_thread.py")
    assert "if not bool(thing.properties.get('play_on_start', False)):" in src
    assert "self.io_manager._execute_input(" in src
    assert "'PlaySound'," in src


def test_sound_radius_gain_is_linear_and_zero_at_edge():
    src = read_source("engine", "qt_game_view.py")
    assert "return 1.0 - (distance / radius)" in src
    assert "if distance >= radius:" in src
    assert "meta.get('radius', 512.0)" in src


def test_global_and_looping_are_independent_speaker_flags():
    src = read_source("editor", "io_handlers.py")
    # Both authored flags are forwarded independently; Global must not disable
    # looping, and Looping must not make a local speaker global.
    assert "'looping': looping," in src
    assert "'global': global_sound," in src


def test_speaker_handlers_send_looping_and_stop():
    """io_handlers must emit the protocol qt_game_view consumes."""
    src = read_source("editor", "io_handlers.py")
    assert "'looping': looping," in src
    assert "'action': 'play'," in src
    assert "{'action': 'stop', 'entity_id': speaker_id}" in src


def test_speaker_entity_declares_the_looping_property():
    src = read_source("editor", "things.py")
    assert "self.properties.setdefault('looping', False)" in src
