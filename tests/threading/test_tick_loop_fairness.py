"""The logic loop shares the tick lock, survives its own faults, and is noticed
when it does not.

The run loop takes ``_tick_lock`` once a frame. CPython's locks are not fair:
a loop that overran its frame took the lock straight back, and the UI thread
-- Stop, Save, Load, a property edit -- could wait seconds for it, or for
ever. Frames also caught up on as many as fifteen ticks in one hold, and the
time another thread held the world (entering Play, a load) was simulated
afterwards. A failed editor-mode tick ran a full Play teardown every frame,
and anything raised outside the tick and the projection ended the thread
with nobody told.

Driven through ``_step_frame`` and ``_yield_to_waiters`` directly wherever
possible; the two tests that need real contention start threads and are
bounded by a deadline.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState          # noqa: E402
from engine import logic_thread as logic_thread_module  # noqa: E402
from engine.logic_thread import LogicThread, TickLock  # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import room  # noqa: E402

pytestmark = pytest.mark.qt

DEADLINE = 5.0


@pytest.fixture
def logic():
    threads = []

    def _build():
        state = EditorState()
        state.brushes = list(room())
        thread = LogicThread(ThreadedGameState(), state)
        threads.append(thread)
        return thread

    yield _build

    for thread in threads:
        thread.stop()
        if thread.is_alive():
            thread.join(timeout=DEADLINE)


def _wait_for(predicate, what):
    end = time.perf_counter() + DEADLINE
    while time.perf_counter() < end:
        if predicate():
            return
        time.sleep(0.001)
    pytest.fail("timed out waiting for " + what)


# ---------------------------------------------------------------------------
# S4: the UI thread gets its turn at the lock
# ---------------------------------------------------------------------------

def test_the_tick_lock_counts_the_threads_blocked_on_it():
    lock = TickLock()
    lock.acquire()
    got = threading.Event()

    def waiter():
        with lock:
            got.set()

    other = threading.Thread(target=waiter, daemon=True)
    other.start()
    _wait_for(lambda: lock.waiters == 1, "the waiter to block")
    assert lock._is_owned()
    lock.release()
    other.join(timeout=DEADLINE)
    assert got.is_set() and lock.waiters == 0
    # Re-entrant, and non-blocking acquire behaves as RLock's.
    with lock:
        with lock:
            assert lock._is_owned()
    assert not lock._is_owned()


def test_the_loop_steps_aside_for_a_thread_waiting_on_the_lock(logic):
    """Released with a waiter blocked, the loop does not take the lock back
    until the waiter has had it."""
    thread = logic()
    thread.running = True
    lock = thread._tick_lock
    order = []
    lock.acquire()                       # the loop, mid-frame

    def ui_thread():
        with lock:
            order.append("ui")

    ui = threading.Thread(target=ui_thread, daemon=True)
    ui.start()
    _wait_for(lambda: lock.waiters == 1, "the UI thread to block on the tick lock")
    lock.release()                       # frame over
    polls = thread._yield_to_waiters()
    with lock:                           # the next frame
        order.append("loop")
    ui.join(timeout=DEADLINE)

    assert order == ["ui", "loop"], "the loop took the lock back past a waiting thread"
    assert polls >= 1


def test_with_nobody_waiting_the_loop_does_not_yield(logic):
    thread = logic()
    thread.running = True
    assert thread._yield_to_waiters() == 0


@pytest.mark.slow
def test_an_overrunning_loop_does_not_starve_the_ui_thread(logic):
    """The audit's reproduction, on the real loop: every tick holds the lock
    and the GIL for 40 ms, so every frame overruns. The UI thread waits at
    most for the frame in progress -- MAX_CATCHUP_TICKS ticks -- where it
    waited for fifteen (0.6 s) and, with a synthetic lock, for ever."""
    import sys
    previous = sys.getswitchinterval()
    sys.setswitchinterval(0.001)
    thread = logic()

    def busy_tick(delta):
        started = time.perf_counter()
        while time.perf_counter() - started < 0.04:
            sum(range(200))

    thread._tick = busy_tick
    thread.start()
    try:
        _wait_for(lambda: thread._tick_count or thread.actual_tps, "the loop to tick")
        waits = []
        for _ in range(6):
            time.sleep(0.05)
            started = time.perf_counter()
            assert thread._tick_lock.acquire(timeout=DEADLINE), (
                "the UI thread could not take the tick lock from an overrunning loop")
            waits.append(time.perf_counter() - started)
            thread._tick_lock.release()
        frame = thread.MAX_CATCHUP_TICKS * 0.04
        assert max(waits) < frame + 0.2, (
            "a UI acquire waited %.3fs; one frame is at most %.2fs" % (max(waits), frame))
    finally:
        sys.setswitchinterval(previous)
        thread.stop()
        thread.join(timeout=DEADLINE)


# ---------------------------------------------------------------------------
# P7: catch-up is capped and the clock resyncs after the world was held
# ---------------------------------------------------------------------------

def _count_ticks(thread):
    ticks = []
    thread._tick = lambda delta: ticks.append(delta)
    return ticks


def test_a_frame_runs_at_most_max_catchup_ticks_and_drops_the_rest(logic):
    thread = logic()
    ticks = _count_ticks(thread)

    left = thread._step_frame(0.25)      # 15 ticks of debt

    assert len(ticks) == thread.MAX_CATCHUP_TICKS
    assert 0.0 <= left < thread.TICK_DURATION, (
        "the debt past the cap was carried into the next frame (%.4f s)" % left)


def test_a_frame_within_the_cap_runs_every_tick_it_holds(logic):
    thread = logic()
    ticks = _count_ticks(thread)
    left = thread._step_frame(3.5 * thread.TICK_DURATION)
    assert len(ticks) == 3
    assert left == pytest.approx(0.5 * thread.TICK_DURATION)


def test_entering_play_resyncs_the_clock_so_setup_time_is_not_simulated(logic):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        assert thread._clock_resync, "entering Play did not ask for a clock resync"
        ticks = _count_ticks(thread)
        thread._step_frame(0.25)
        assert len(ticks) == 1, (
            "the frame after Play started caught up %d ticks of setup time" % len(ticks))
        assert thread._clock_resynced and not thread._clock_resync
        ticks.clear()
        thread._step_frame(0.25)         # an ordinary frame again: the cap
        assert len(ticks) == thread.MAX_CATCHUP_TICKS
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_a_long_wait_for_the_lock_resyncs_the_clock(logic, monkeypatch):
    thread = logic()
    ticks = _count_ticks(thread)
    clock = iter([0.0, 1.0] + [1.0] * 100)      # entered at 0, got the lock at 1 s
    monkeypatch.setattr(logic_thread_module.time, "perf_counter", lambda: next(clock))
    thread._step_frame(0.25)
    assert len(ticks) == 1
    assert thread._clock_resynced


# ---------------------------------------------------------------------------
# S15: a failing editor tick is logged once and tears nothing down
# ---------------------------------------------------------------------------

def test_a_failing_editor_tick_neither_tears_down_play_nor_floods_the_log(logic, monkeypatch):
    thread = logic()
    logged = []
    monkeypatch.setattr(logic_thread_module, "debug_log",
                        lambda category, message: logged.append(message))
    teardowns = []
    thread.set_gui_fault_teardown(teardowns.append)
    stops = []
    monkeypatch.setattr(thread.session_runtime, "apply_play_mode",
                        lambda enabled, *a, **k: stops.append(enabled))

    def explode(delta):
        raise RuntimeError("editor tick failure")

    thread._tick = explode
    for _ in range(120):                 # two seconds of frames
        thread._step_frame(thread.TICK_DURATION)

    assert teardowns == [], "an editor-mode fault queued a Play teardown"
    assert stops == [], "an editor-mode fault ran apply_play_mode(False) %d times" % len(stops)
    tick_logs = [m for m in logged if "editor tick failure" in m]
    assert len(tick_logs) == 1, "the same failure was logged %d times" % len(tick_logs)
    assert not thread._tick_faulted


def test_a_failing_play_tick_queues_one_teardown(logic, monkeypatch):
    thread = logic()
    monkeypatch.setattr(logic_thread_module, "debug_log", lambda *a: None)
    thread.session_runtime.play_mode = True
    teardowns = []
    thread.set_gui_fault_teardown(teardowns.append)
    stops = []
    monkeypatch.setattr(thread.session_runtime, "apply_play_mode",
                        lambda enabled, *a, **k: stops.append(enabled))

    def explode(delta):
        raise RuntimeError("play tick failure")

    thread._tick = explode
    for _ in range(10):
        thread._step_frame(thread.TICK_DURATION)

    assert len(teardowns) == 1
    assert stops == [], "the GUI host's teardown ran on the worker as well"
    assert thread._tick_faulted


def test_a_headless_play_fault_ends_play_once(logic, monkeypatch):
    thread = logic()
    monkeypatch.setattr(logic_thread_module, "debug_log", lambda *a: None)
    thread.session_runtime.apply_play_mode(True)

    def explode(delta):
        raise RuntimeError("play tick failure")

    thread._tick = explode
    for _ in range(10):
        thread._step_frame(thread.TICK_DURATION)
    assert thread.session_runtime.play_mode is False


# ---------------------------------------------------------------------------
# S16: the loop survives what the frame guards miss, and a death is noticed
# ---------------------------------------------------------------------------

def test_an_exception_outside_the_tick_does_not_end_the_thread(logic, monkeypatch):
    thread = logic()
    logged = []
    monkeypatch.setattr(logic_thread_module, "debug_log",
                        lambda category, message: logged.append(message))
    publishes = []

    def bad_publish():
        publishes.append(1)
        raise RuntimeError("publication failure")

    thread._publish_frame = bad_publish
    thread.start()
    try:
        _wait_for(lambda: len(publishes) >= 3, "the loop to keep running")
        assert thread.is_alive(), "an exception in publication ended the logic thread"
    finally:
        thread.stop()
        thread.join(timeout=DEADLINE)
    assert sum("publication failure" in m for m in logged) == 1


def test_a_thread_that_dies_records_why(logic, monkeypatch):
    thread = logic()
    monkeypatch.setattr(logic_thread_module, "debug_log", lambda *a: None)

    def broken_loop():
        raise RuntimeError("loop bookkeeping failure")

    thread._run_loop = broken_loop
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    thread.start()
    thread.join(timeout=DEADLINE)
    assert not thread.is_alive()
    assert thread.running, "the thread was not stopped; it died"
    assert "loop bookkeeping failure" in thread.death_message


def test_the_view_reports_a_dead_logic_thread_once_and_leaves_play(
        logic, main_window, monkeypatch):
    from engine import qt_game_view

    logged = []
    monkeypatch.setattr(qt_game_view, "debug_log",
                        lambda category, message: logged.append((category, message)))
    monkeypatch.setattr(logic_thread_module, "debug_log", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    dead = logic()

    def broken_loop():
        raise RuntimeError("it broke")

    dead._run_loop = broken_loop
    dead.start()
    dead.join(timeout=DEADLINE)
    assert not dead.is_alive() and dead.running

    view = main_window.view_3d
    window = main_window
    exits, toasts = [], []
    monkeypatch.setattr(view, "logic_thread", dead)
    monkeypatch.setattr(view, "_thread_started", True)
    monkeypatch.setattr(view, "play_mode", True)
    monkeypatch.setattr(window, "_exit_play_mode", lambda: exits.append(1))
    monkeypatch.setattr(window, "show_toast", lambda *a, **k: toasts.append(a))

    assert view._check_logic_thread_health() is False
    assert view._check_logic_thread_health() is False
    assert len(logged) == 1 and logged[0][0] == "Error"
    assert "it broke" in logged[0][1]
    assert exits == [1] and len(toasts) == 1

    stopped = logic()
    stopped.stop()                       # never started, stopped on purpose
    monkeypatch.setattr(view, "logic_thread", stopped)
    assert view._check_logic_thread_health() is True, (
        "a thread stopped on purpose was reported as dead")
