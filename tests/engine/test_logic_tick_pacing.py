"""The logic thread's loop runs each tick in its own 60 Hz slot.

The real :class:`LogicThread`'s ``run`` loop, driven on a fake clock:
``time.perf_counter`` reads it and ``time.sleep`` advances it (with a little
oversleep, as a real timer has), and the frame's stepping and publication
are replaced by stand-ins that cost what they cost on the real thread.
Sleeping a fixed share of a tick from the start of each iteration -- the old
loop -- put ticks up to ~14 ms off their slots whenever an iteration began
just short of a tick."""

import pytest

pytest.importorskip("PyQt5", reason="the logic thread holds the editor state")

from editor.editor_state import EditorState               # noqa: E402
from engine import logic_thread as lt                     # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def logic():
    thread = LogicThread(ThreadedGameState(), EditorState())
    yield thread
    thread.stop()


def _drive(logic, monkeypatch, *, tick_cost, publish_cost, oversleep, ticks=240):
    clock = [100.0]
    starts = []
    iterations = [0]

    def perf_counter():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds + oversleep

    tick = LogicThread.TICK_DURATION

    def step_frame(accumulator):
        iterations[0] += 1
        while accumulator >= tick:
            starts.append(clock[0])
            clock[0] += tick_cost
            accumulator -= tick
        clock[0] += 0.0001                     # project the frame
        if len(starts) >= ticks:
            logic.running = False
        return accumulator

    def publish_frame():
        clock[0] += publish_cost
        return True

    monkeypatch.setattr(logic, "_step_frame", step_frame)
    monkeypatch.setattr(logic, "_publish_frame", publish_frame)
    monkeypatch.setattr(lt.time, "perf_counter", perf_counter)
    monkeypatch.setattr(lt.time, "sleep", sleep)
    logic.running = True
    logic.run()                                 # the real loop, on this thread
    return starts, iterations[0]


@pytest.mark.parametrize("tick_cost,publish_cost,oversleep", [
    (0.0003, 0.00005, 0.0),
    (0.0003, 0.00005, 0.0008),      # a coarse timer
    (0.004, 0.0005, 0.0003),        # a heavy tick
])
def test_each_tick_lands_in_its_own_slot(logic, monkeypatch, tick_cost, publish_cost,
                                         oversleep):
    starts, iterations = _drive(logic, monkeypatch, tick_cost=tick_cost,
                                publish_cost=publish_cost, oversleep=oversleep)
    tick = LogicThread.TICK_DURATION
    intervals = [b - a for a, b in zip(starts[10:], starts[11:])]
    worst = max(abs(i - tick) for i in intervals)
    # Off its slot by no more than the timer's oversleep and the wake margin.
    assert worst <= oversleep + LogicThread.WAKE_MARGIN + 1e-6, worst
    # One tick an iteration: no iteration wakes early and runs none.
    assert iterations <= len(starts) + 2


def test_the_rate_holds_at_sixty_ticks_a_second(logic, monkeypatch):
    starts, _ = _drive(logic, monkeypatch, tick_cost=0.0003, publish_cost=0.00005,
                       oversleep=0.0005, ticks=600)
    rate = (len(starts) - 1) / (starts[-1] - starts[0])
    assert rate == pytest.approx(1.0 / LogicThread.TICK_DURATION, rel=1e-3)
