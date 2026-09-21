"""``MonsterAIThread``: the background thread MonsterAI runs on.

This is the one place in the MonsterAI area that deliberately uses real threads
and real time, because thread lifetime is what is under test.  Everything is
bounded by an explicit deadline and polls for a *condition* rather than sleeping
a fixed amount, so the tests do not become slower or flakier on a loaded
machine.

The hazards being guarded are the ones that are invisible in play: a thread
that keeps ticking after ``stop()``, a second thread started without the first
being stopped, and an exception on the AI thread taking the thread down while
the game carries on with monsters that have quietly stopped moving.
"""

import threading
import time

import pytest

from engine.monster_ai import MonsterAI, MonsterAIThread
from tests.helpers.fakes import FakeLogicThread, FakePlayer

pytestmark = [pytest.mark.qt, pytest.mark.slow]

#: Nothing here may hang the suite.  Every wait is bounded by this.
DEADLINE = 5.0


def _wait_for(predicate, timeout=DEADLINE, what="condition"):
    """Poll until ``predicate()`` is true, or fail with what was still false."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.005)
    pytest.fail("timed out after %.1fs waiting for %s" % (timeout, what))


class CountingAI:
    """Stands in for MonsterAI so a tick is observable and instant."""

    def __init__(self):
        self.ticks = 0
        self.deltas = []
        self.raise_on_tick = None
        self.block = None          # an Event the tick waits on, if set

    def update(self, delta):
        if self.block is not None:
            self.block.wait(DEADLINE)
        self.ticks += 1
        self.deltas.append(delta)
        if self.raise_on_tick is not None and self.ticks == self.raise_on_tick:
            raise RuntimeError("deliberate failure on tick %d" % self.ticks)


@pytest.fixture
def ai_thread():
    """Starts threads and guarantees they are stopped and joined afterwards."""
    started = []

    def _start(ai=None, tick_rate=120, lock=None):
        thread = MonsterAIThread(FakeLogicThread(), ai or CountingAI(),
                                 lock or threading.RLock(), tick_rate=tick_rate)
        started.append(thread)
        thread.start()
        return thread

    yield _start

    for thread in started:
        thread.stop()
        thread.join(timeout=DEADLINE)


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def test_a_started_thread_ticks(ai_thread):
    ai = CountingAI()
    thread = ai_thread(ai)
    _wait_for(lambda: ai.ticks > 0, what="the AI thread's first tick")
    assert thread.running is True


def test_the_tick_delta_is_the_configured_fixed_step(ai_thread):
    """A fixed timestep is what makes the AI reproducible frame to frame."""
    ai = CountingAI()
    ai_thread(ai, tick_rate=50)
    _wait_for(lambda: len(ai.deltas) >= 3, what="three ticks")
    assert set(ai.deltas[:3]) == {1.0 / 50}, (
        "ticks were delivered with deltas %s; a 50 Hz thread must always pass "
        "1/50" % (sorted(set(ai.deltas[:3])),))


def test_stop_ends_the_thread(ai_thread):
    ai = CountingAI()
    thread = ai_thread(ai)
    _wait_for(lambda: ai.ticks > 0, what="the first tick")

    thread.stop()
    thread.join(timeout=DEADLINE)

    assert thread.is_alive() is False, (
        "the AI thread was still alive %.1fs after stop()" % DEADLINE)
    assert thread.running is False


def test_no_ticks_happen_after_the_thread_has_joined(ai_thread):
    ai = CountingAI()
    thread = ai_thread(ai)
    _wait_for(lambda: ai.ticks > 0, what="the first tick")

    thread.stop()
    thread.join(timeout=DEADLINE)
    settled = ai.ticks
    time.sleep(0.1)

    assert ai.ticks == settled, (
        "the AI ticked %d more times after the thread was joined - it is "
        "still mutating the world behind the editor's back"
        % (ai.ticks - settled))


def test_stopping_twice_is_harmless(ai_thread):
    thread = ai_thread()
    thread.stop()
    thread.stop()
    thread.join(timeout=DEADLINE)
    assert thread.is_alive() is False


def test_stopping_a_thread_that_never_started_is_harmless():
    thread = MonsterAIThread(FakeLogicThread(), CountingAI(), threading.RLock())
    thread.stop()
    assert thread.running is False
    assert thread.is_alive() is False


def test_a_restarted_ai_runs_on_a_fresh_thread(ai_thread):
    first_ai, second_ai = CountingAI(), CountingAI()
    first = ai_thread(first_ai)
    _wait_for(lambda: first_ai.ticks > 0, what="the first thread's tick")
    first.stop()
    first.join(timeout=DEADLINE)
    quiesced = first_ai.ticks

    second = ai_thread(second_ai)
    _wait_for(lambda: second_ai.ticks > 0, what="the restarted thread's tick")

    assert second is not first
    assert first_ai.ticks == quiesced, \
        "the stopped thread resumed ticking after a restart"


def test_the_thread_is_a_daemon_so_it_cannot_hold_the_process_open():
    thread = MonsterAIThread(FakeLogicThread(), CountingAI(), threading.RLock())
    assert thread.daemon is True, (
        "a non-daemon AI thread would keep Fio alive after the window closed")
    assert thread.name == "MonsterAIThread", (
        "the thread should be identifiable in a stack dump; it is named %r"
        % thread.name)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------

def test_every_tick_is_taken_under_the_shared_lock(ai_thread):
    """The logic thread holds the same lock while it rebuilds the world."""
    class LockRecorder:
        def __init__(self):
            self.real = threading.RLock()
            self.held_during_tick = []
            self.depth = 0

        def __enter__(self):
            self.real.acquire()
            self.depth += 1
            return self

        def __exit__(self, *exc):
            self.depth -= 1
            self.real.release()
            return False

    lock = LockRecorder()

    class Watcher(CountingAI):
        def update(self, delta):
            lock.held_during_tick.append(lock.depth)
            super().update(delta)

    ai = Watcher()
    ai_thread(ai, lock=lock)
    _wait_for(lambda: ai.ticks > 0, what="a tick")
    assert all(d >= 1 for d in lock.held_during_tick), (
        "MonsterAI.update ran outside the shared lock (depths seen: %s)"
        % (lock.held_during_tick[:5],))


def test_holding_the_lock_keeps_the_ai_out(ai_thread):
    """What the editor relies on when it swaps the world under the AI."""
    lock = threading.RLock()
    ai = CountingAI()
    ai_thread(ai, lock=lock)
    _wait_for(lambda: ai.ticks > 0, what="a tick")

    with lock:
        settled = ai.ticks
        time.sleep(0.1)
        blocked = ai.ticks

    assert blocked == settled, (
        "the AI ticked %d times while another thread held the lock"
        % (blocked - settled))


# ---------------------------------------------------------------------------
# Failure
# ---------------------------------------------------------------------------

@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_exception_on_the_ai_thread_stops_that_thread_and_nothing_else(
        ai_thread):
    """Documents what actually happens today so a change is a decision.

    ``MonsterAIThread.run`` does not guard the tick, so an exception ends the
    thread: monsters silently stop moving while the game carries on.  The test
    pins both halves - the thread dies, and the rest of the process does not -
    so that adding a guard is a visible change rather than a silent one.
    """
    ai = CountingAI()
    ai.raise_on_tick = 3
    thread = ai_thread(ai)

    _wait_for(lambda: not thread.is_alive(),
              what="the AI thread to end after its tick raised")

    assert ai.ticks == 3, (
        "the thread should have ended on the raising tick; it reached tick %d"
        % ai.ticks)
    assert threading.main_thread().is_alive(), \
        "the failure escaped the AI thread"
    # ``running`` is still True: nothing cleared it, which is how a caller can
    # tell this thread died rather than being stopped.
    assert thread.running is True, (
        "a thread that died in its tick still reads as running; that is the "
        "only signal a caller has that it was not stopped deliberately")
