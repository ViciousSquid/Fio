"""A simulation tick that raises ends Play exactly as Stop does.

The logic thread marks the fault and queues the teardown to the GUI thread;
there it must take the editor's Stop path, so a world captured at Play is put
back and the editor is left in editor mode, not half in Play.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                  # noqa: E402

from editor.things import Monster, PlayerStart              # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]


def _arena():
    things = [make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0),
              make_thing(Monster, "m0", (0.0, 40.0, 600.0))]
    return level_data(brushes=room(size=2048.0, height=512.0), things=things)


def test_a_failed_tick_stops_play_through_the_editors_stop(fio_session, monkeypatch):
    session = fio_session(_arena())
    window = session.window
    window.config.set("Settings", "restore_world_on_stop", "True")
    authored = window.state.snapshot()
    session.start_play()
    assert window.play_button.text() == "Stop"
    session.step(30, keys={Qt.Key_W})               # the world moves in play

    def explode():
        raise RuntimeError("deliberate tick failure")

    monkeypatch.setattr(session.logic.combat_runtime, "_update_bullet_marks", explode)
    session.step(1)
    assert session.logic._tick_faulted
    session.app.processEvents()                     # the queued GUI teardown

    assert not session.playing
    assert not session.logic.session_runtime.play_mode
    assert window.play_button.text() != "Stop"
    assert window.state.snapshot() == authored, "the world captured at Play was not put back"
    monkeypatch.undo()
    session.start_play().step(5)                    # and Play works again
    assert session.playing and not session.logic._tick_faulted
