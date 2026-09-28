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
