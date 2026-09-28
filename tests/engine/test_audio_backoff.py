"""A machine with no working audio device must not stall on every sound.

A failed ``pygame.mixer.init`` probes the audio stack for about 100 ms on the
UI thread, and every sound request used to try again: each gunshot, door and
explosion cost a frame. The failure is now remembered and the device retried
only after a pause, so one connected later is still found.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="the view is a Qt widget")
pygame = pytest.importorskip("pygame")

from engine import qt_game_view                               # noqa: E402

pytestmark = pytest.mark.qt


class _FailingMixer:
    def __init__(self):
        self.attempts = 0

    def get_init(self):
        return False

    def init(self, **_kwargs):
        self.attempts += 1
        raise pygame.error("no audio device")


def test_a_failed_mixer_is_not_reprobed_on_every_sound(monkeypatch):
    mixer = _FailingMixer()
    monkeypatch.setattr(qt_game_view.pygame, "mixer", mixer)
    clock = [100.0]
    monkeypatch.setattr(qt_game_view.time, "perf_counter", lambda: clock[0])
    view = SimpleNamespace(
        MIXER_RETRY_SECONDS=qt_game_view.QtGameView.MIXER_RETRY_SECONDS)
    ensure = qt_game_view.QtGameView._ensure_pygame_mixer

    for _ in range(20):
        assert ensure(view) is False
    assert mixer.attempts == 1

    clock[0] += view.MIXER_RETRY_SECONDS
    ensure(view)
    assert mixer.attempts == 2, "a device connected later would never be found"
