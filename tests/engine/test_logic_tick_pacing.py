"""The logic thread's loop runs each tick in its own 60 Hz slot.

Driven on a fake clock: ``time.perf_counter`` reads it and ``time.sleep``
advances it (with a little oversleep, as a real timer has), and the tick and
publication cost what they cost on the real thread. Sleeping a fixed share
of a tick from the start of each iteration -- the old loop -- put ticks up to
~14 ms off their slots whenever an iteration began just short of a tick."""

import types

import pytest

from engine import logic_thread as lt
from engine.logic_thread import LogicThread


def _drive(monkeypatch, *, tick_cost, publish_cost, oversleep, ticks=240):
    clock = [100.0]
    starts = []
    iterations = [0]

    def perf_counter():
        return clock[0]

    def sleep(seconds):
        clock[0] += seconds + oversleep

    host = types.SimpleNamespace(running=True, TICK_DURATION=LogicThread.TICK_DURATION,
                                 WAKE_MARGIN=LogicThread.WAKE_MARGIN)

    def step_frame(accumulator):
        iterations[0] += 1
        while accumulator >= host.TICK_DURATION:
            starts.append(clock[0])
            clock[0] += tick_cost
            accumulator -= host.TICK_DURATION
        clock[0] += 0.0001                     # project the frame
        if len(starts) >= ticks:
            host.running = False
        return accumulator

    def publish_frame():
        clock[0] += publish_cost
        return True

    host._step_frame = step_frame
    host._publish_frame = publish_frame
    monkeypatch.setattr(lt.time, "perf_counter", perf_counter)
    monkeypatch.setattr(lt.time, "sleep", sleep)
    LogicThread.run(host)
    return starts, iterations[0]


@pytest.mark.parametrize("tick_cost,publish_cost,oversleep", [
    (0.0003, 0.00005, 0.0),
    (0.0003, 0.00005, 0.0008),      # a coarse timer
    (0.004, 0.0005, 0.0003),        # a heavy tick
])
def test_each_tick_lands_in_its_own_slot(monkeypatch, tick_cost, publish_cost, oversleep):
    starts, iterations = _drive(monkeypatch, tick_cost=tick_cost,
                                publish_cost=publish_cost, oversleep=oversleep)
    tick = LogicThread.TICK_DURATION
    intervals = [b - a for a, b in zip(starts[10:], starts[11:])]
    worst = max(abs(i - tick) for i in intervals)
    # Off its slot by no more than the timer's oversleep and the wake margin.
    assert worst <= oversleep + LogicThread.WAKE_MARGIN + 1e-6, worst
    # One tick an iteration: no iteration wakes early and runs none.
    assert iterations <= len(starts) + 2


def test_the_rate_holds_at_sixty_ticks_a_second(monkeypatch):
    starts, _ = _drive(monkeypatch, tick_cost=0.0003, publish_cost=0.00005,
                       oversleep=0.0005, ticks=600)
    rate = (len(starts) - 1) / (starts[-1] - starts[0])
    assert rate == pytest.approx(1.0 / LogicThread.TICK_DURATION, rel=1e-3)
