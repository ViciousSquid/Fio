"""Behaviour-preservation tests for the mover/door scalar maths, the looping
speaker queue protocol, and the floating-window manager."""

import os
import random
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tests.helpers.paths import read_source  # noqa: E402

pytestmark = pytest.mark.qt


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
    src = read_source("engine", "logic_movers.py")
    assert '"_direction_np": (' in src
    assert "float(direction[0])" in src
    # Resolved lazily on the first tick, now in the dense mover table.
    from engine.mover_table import _unit_direction
    direction = _unit_direction([0, 3, 4])
    assert type(direction) is tuple and all(type(v) is float for v in direction)
    assert direction == (0.0, 0.6, 0.8)
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
    """Leaf audio-device stand-in; production QtGameView does the queue work."""

    def __init__(self):
        self.plays = []

    def play(self, loops=0):
        channel = _FakeChannel()
        self.plays.append((loops, channel))
        return channel


def _real_speaker(main_window, monkeypatch, requests, sound):
    view = main_window.view_3d
    view.stop_all_sounds()
    view.game_state.queue_sound(requests[0]) if requests else None
    for request in requests[1:]:
        view.game_state.queue_sound(request)
    monkeypatch.setattr(view, "_get_sound_instance", lambda _name: sound)
    return view


def test_looping_speaker_plays_with_infinite_loops(main_window, monkeypatch):
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{
            "action": "play",
            "file": "hum.wav",
            "volume": 0.5,
            "looping": True,
            "entity_id": 1,
        }],
        snd,
    )
    view._process_sound_queue()
    assert snd.plays[0][0] == -1
    assert snd.plays[0][1].volume == 0.5
    assert 1 in view._speaker_channels


def test_non_looping_speaker_plays_once_and_is_not_tracked(main_window, monkeypatch):
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{
            "action": "play",
            "file": "ding.wav",
            "looping": False,
            "entity_id": 2,
        }],
        snd,
    )
    view._process_sound_queue()
    assert snd.plays[0][0] == 0
    assert 2 not in view._speaker_channels


def test_stop_sound_silences_a_looping_channel(main_window, monkeypatch):
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{
            "action": "play",
            "file": "hum.wav",
            "looping": True,
            "entity_id": 3,
        }],
        snd,
    )
    view._process_sound_queue()
    channel = view._speaker_channels[3]
    view.game_state.queue_sound({"action": "stop", "entity_id": 3})
    view._process_sound_queue()
    assert channel.stopped
    assert 3 not in view._speaker_channels


def test_retriggering_a_looping_speaker_does_not_stack(main_window, monkeypatch):
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{
            "action": "play",
            "file": "hum.wav",
            "looping": True,
            "entity_id": 4,
        }],
        snd,
    )
    view._process_sound_queue()
    first = view._speaker_channels[4]
    view.game_state.queue_sound({
        "action": "play",
        "file": "hum.wav",
        "looping": True,
        "entity_id": 4,
    })
    view._process_sound_queue()
    assert first.stopped
    assert view._speaker_channels[4] is not first


def test_stop_for_an_unknown_entity_is_harmless(main_window, monkeypatch):
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{"action": "stop", "entity_id": 999}],
        snd,
    )
    view._process_sound_queue()


def test_legacy_request_without_action_still_plays(main_window, monkeypatch):
    """Existing callers queue a bare {'file','volume'} dict."""
    snd = _FakeSound()
    view = _real_speaker(
        main_window,
        monkeypatch,
        [{"file": "splash.wav", "volume": 0.8}],
        snd,
    )
    view._process_sound_queue()
    assert snd.plays[0][0] == 0
    assert snd.plays[0][1].volume == 0.8

def test_speaker_handlers_queue_spatial_radius_data():
    src = read_source("editor", "io_handlers.py")
    assert "'position': position," in src
    assert "'radius': radius," in src
    assert "'global': global_sound," in src


def test_speaker_start_on_is_consumed_at_player_spawn():
    src = read_source("engine", "logic_session.py")
    assert 'if not bool(thing.properties.get("play_on_start", False)):' in src
    assert "logic.io_manager._execute_input(" in src
    assert "PlaySound" in src


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
