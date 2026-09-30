"""The Debug Tables instrument must not hold the renderer's frame.

Publication is double-buffered: while anything borrows the published frame,
the logic thread cannot swap. The instrument used to keep the snapshot it
sampled until its next refresh, a quarter of a second later, and so held a
borrow permanently -- with it open, the renderer never saw another frame.
"""

from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the instrument is a Qt window")

from engine.threaded_game_state import ThreadedGameState      # noqa: E402
from tests.helpers.worlds import box_brush                    # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def window(qt_app):
    from PyQt5.QtWidgets import QMainWindow
    from tools.debug_tables import DebugTablesWindow

    game_state = ThreadedGameState()
    write = game_state.get_write_state()
    write.render_table.sync([box_brush("wall")], 1)
    write.visible_brush_slots = np.array([0], dtype=np.int32)
    write.prepare_ms = 1.5
    assert game_state.request_swap() is True

    host = QMainWindow()
    host.view_3d = SimpleNamespace(
        logic_thread=SimpleNamespace(game_state=game_state),
        renderer=None, paint_ms=4.0)
    host.state = SimpleNamespace(selected_object=None, selected_objects=[])
    instrument = DebugTablesWindow(host)
    instrument.timer.stop()
    yield instrument, game_state
    instrument.close()


def test_sampling_does_not_hold_the_published_frame(window):
    instrument, game_state = window
    instrument.refresh()

    assert game_state.request_swap() is True, (
        "the instrument is still borrowing the frame it sampled, so the "
        "logic thread can never publish another")


def test_what_it_shows_is_a_copy_of_the_sampled_frame(window):
    instrument, game_state = window
    instrument.refresh()
    shown = instrument.render

    game_state.request_swap()                 # both buffers change hands
    game_state.get_write_state().render_table.center[0] = 999.0

    assert shown.count == 1
    assert shown.center[0].tolist() != [999.0, 999.0, 999.0]
    assert "prepare (logic thread)" in instrument.dashboard.toPlainText()
    assert "1.500 ms" in instrument.dashboard.toPlainText()


@pytest.fixture
def monster_window(window):
    """The instrument attached to a monster AI that has run one dense tick."""
    import threading

    from editor.things import Monster
    from engine.monster_ai import MonsterAI
    from tests.helpers.fakes import FakeLogicThread, FakePlayer
    from tests.helpers.worlds import make_thing

    instrument, game_state = window
    things = [make_thing(Monster, "m%d" % i, (300.0 * (i + 1), 96, 0),
                         monster_type="human", awake=True, team="red")
              for i in range(3)]
    things.append(make_thing(Monster, "corpse", (0, 96, 900), dead=True))
    logic = FakeLogicThread(brushes=[box_brush("ground", (0, -16, 0), (8192, 32, 8192))],
                            things=things, player=FakePlayer((0.0, 0.0, 0.0)))
    logic._monster_things = list(things)
    ai = MonsterAI(logic)
    ai.set_spatial_grid(logic.build_spatial_grid())
    ai.update(1.0 / 30.0)
    view = instrument.main_window.view_3d
    view.logic_thread.monster_ai = ai
    view.logic_thread._monster_lock = threading.RLock()
    view.logic_thread.monster_ai_thread = SimpleNamespace(
        update_ms=2.0, lock_wait_ms=0.5)
    return instrument, ai, view.logic_thread._monster_lock


def test_the_monster_table_is_shown_after_a_dense_tick(monster_window):
    instrument, ai, _ = monster_window
    instrument.refresh()

    shown = instrument.monsters
    assert shown is not None and shown.count == 4
    assert shown.path == "dense"
    assert shown.mode[3] == 1                     # the corpse: MODE_DEAD
    text = instrument.dashboard.toPlainText()
    assert "MONSTER AI (MonsterTable)" in text
    assert "pass                 dense" in text
    assert "dead 1" in text
    assert "waiting for the monster lock    0.500 ms" in text
    assert instrument.monster_raw.selector.count() > 0
    assert "MonsterTable" in instrument.memory_text.toPlainText()


def test_a_busy_monster_lock_is_never_waited_on(monster_window):
    import threading

    instrument, ai, lock = monster_window
    instrument.refresh()
    before = instrument.monsters

    held, release = threading.Event(), threading.Event()

    def ai_tick():
        with lock:
            held.set()
            release.wait(5.0)

    worker = threading.Thread(target=ai_tick)
    worker.start()
    held.wait(5.0)
    try:
        instrument.refresh()                      # returns: does not block
        assert instrument.monsters is before      # the last copy is kept
    finally:
        release.set()
        worker.join()


def test_the_export_includes_the_monster_table(monster_window, tmp_path):
    import json
    import zipfile

    instrument, ai, _ = monster_window
    instrument.refresh()
    path = tmp_path / "snapshot.zip"
    from unittest import mock
    with mock.patch("tools.debug_tables.QFileDialog.getSaveFileName",
                    return_value=(str(path), "")):
        instrument.export_snapshot()
    assert "EXPORT FAILED" not in instrument.status.text(), instrument.status.text()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        pipeline = json.loads(archive.read("pipeline.json"))
    assert "MonsterTable/mode.npy" in names
    assert pipeline["monster_rows"] == 4
    assert pipeline["monster_pass"] == "dense"


def test_export_writes_every_table_of_a_real_frame(window, tmp_path):
    """The tables' ``refs`` columns hold objects, which ``np.save`` refuses;
    they used to abort the export after the first table."""
    import zipfile
    from unittest import mock

    instrument, _ = window
    instrument.refresh()
    path = tmp_path / "snapshot.zip"
    with mock.patch("tools.debug_tables.QFileDialog.getSaveFileName",
                    return_value=(str(path), "")):
        instrument.export_snapshot()
    assert "EXPORT FAILED" not in instrument.status.text(), instrument.status.text()
    with zipfile.ZipFile(path) as archive:
        tables = {n.split("/")[0] for n in archive.namelist() if n.endswith(".npy")}
    assert {"RenderTable", "EntityTable"} <= tables


def test_follow_selection_names_the_render_row_key_and_run(window):
    instrument, game_state = window
    brush = game_state._read_state.render_table.brushes[0]
    instrument.main_window.state = SimpleNamespace(
        selected_object=brush, selected_objects=[brush])
    instrument.refresh()
    status = instrument.status.text()
    assert "FOLLOW id=%s" % brush["id"] in status
    assert "render-row=0" in status and "key=0x" in status and "run=0" in status
