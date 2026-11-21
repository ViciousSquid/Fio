"""Stop leaves no game sound behind, even one queued while Play was ending.

The view stopped every channel and cleared the sound queue first thing on
Stop, then tore the session down. The last frame and the MonsterAI thread can
still queue sounds in that window; a looping speaker started from the queue
afterwards played on in the editor.
"""

import pytest

pytest.importorskip("PyQt5")

from editor.things import PlayerStart                       # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]


def test_a_sound_queued_during_teardown_does_not_survive_stop(fio_session, monkeypatch):
    session = fio_session(level_data(
        brushes=room(), things=[make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0))]))
    session.start_play()
    view = session.window.view_3d
    runtime = session.logic.session_runtime
    real = runtime.apply_play_mode

    def teardown_with_a_late_sound(enabled, *args, **kwargs):
        # What the AI thread or the last frame does while Play is ending.
        session.game_state.queue_sound({"action": "play", "file": "hum.wav",
                                        "looping": True, "entity_id": 7})
        return real(enabled, *args, **kwargs)

    monkeypatch.setattr(runtime, "apply_play_mode", teardown_with_a_late_sound)
    session.stop_play()

    assert list(session.game_state.consume_sounds()) == [], (
        "a sound queued during teardown was still waiting to play in the editor")
    assert not getattr(view.audio, "_speaker_channels", {})
