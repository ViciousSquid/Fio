"""Save a play session from the console, play on, load it back: same world.

Through the in-game console (the command line a player types into), the
session runtime's save and load, and the savegame format on disk; the
restored world is checked in the published frame against what was published
at the moment of saving.
"""

import os
import uuid

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                  # noqa: E402

from editor.things import Monster, PlayerStart              # noqa: E402
from engine import savegame                                  # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]


def _arena():
    things = [make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)]
    things += [make_thing(Monster, "m%d" % i, (x, 40.0, 700.0))
               for i, x in enumerate((-500.0, 0.0, 500.0))]
    return level_data(brushes=room(size=2048.0, height=512.0), things=things)


def _console(session, line):
    console = session.view.debug_console_window
    console.command_input.setText(line)
    console._on_command_entered()


def _world(session):
    session.publish()
    with session.render_state() as frame:
        rows = frame.entity_table
        monsters = {
            t.properties["id"]: tuple(round(float(v), 2)
                                      for v in rows.pos[rows.slot_of_id[t.properties["id"]]])
            for t in session.state.things if isinstance(t, Monster)}
        return (tuple(round(float(v), 2) for v in frame.player_pos),
                round(float(frame.player_angle), 4), frame.player_health, monsters)


def test_console_save_then_load_restores_the_published_world(fio_session):
    session = fio_session(_arena()).start_play()
    name = "e2e_%s" % uuid.uuid4().hex[:8]
    path = os.path.join(session.window.root_dir, "saves", name + ".fiosave")
    try:
        session.step(40, keys={Qt.Key_W}, mouse=(3.0, 0.0))
        saved = _world(session)
        _console(session, "save " + name)
        assert os.path.isfile(path), "save wrote nothing"
        savegame.read(path)                 # passes the save validator

        session.step(60, keys={Qt.Key_W, Qt.Key_A}, mouse=(-7.0, 0.0))
        moved = _world(session)
        assert moved[0] != saved[0] and moved[3] != saved[3]

        _console(session, "load " + name)
        assert session.playing
        assert _world(session) == saved
    finally:
        if os.path.exists(path):
            os.remove(path)
