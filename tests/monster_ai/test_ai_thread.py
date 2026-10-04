"""MonsterAIThread lifecycle against the real MonsterAI machinery.

The thread tests observe a production MonsterAI rather than substituting a
fake AI. The observer records tick timing and can inject one controlled
failure, but every non-fault tick executes MonsterAI.update() itself.
"""

import threading
import time

import pytest

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.monster_ai import MonsterAIThread
from engine.threaded_game_state import ThreadedGameState

pytestmark = [pytest.mark.qt, pytest.mark.slow]
DEADLINE = 5.0


def _wait_for(predicate, timeout=DEADLINE, what="condition"):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.005)
    pytest.fail("timed out after %.1fs waiting for %s" % (timeout, what))


@pytest.fixture
def ai_thread():
    """Start real MonsterAIThread instances around real LogicThread owners."""
    sessions = []

    def start(tick_rate=120, lock=None):
        logic = LogicThread(ThreadedGameState(), EditorState())
        ai = logic.monster_ai
        state = {
            "ticks": 0,
            "deltas": [],
            "raise_on_tick": None,
            "block": None,
            "before_update": None,
        }
        real_update = ai.update

        def observed_update(delta):
            tick_no = state["ticks"] + 1
            block = state["block"]
            if block is not None:
                block.wait(DEADLINE)
            callback = state["before_update"]
            if callback is not None:
                callback()
            state["ticks"] = tick_no
            state["deltas"].append(delta)
            if state["raise_on_tick"] == tick_no:
                raise RuntimeError("deliberate failure on tick %d" % tick_no)
            return real_update(delta)

        ai.update = observed_update
        thread = MonsterAIThread(
            logic, ai, lock or logic.session_runtime.monster_lock,
            tick_rate=tick_rate)
        sessions.append((thread, logic, state, ai))
        thread.start()
        return thread, logic, state, ai

    yield start

    for thread, logic, _state, _ai in sessions:
        thread.stop()
        thread.join(timeout=DEADLINE)
        logic.stop()


def test_a_started_thread_ticks(ai_thread):
    thread, _logic, state, _ai = ai_thread()
    _wait_for(lambda: state["ticks"] > 0, what="the AI thread's first tick")
    assert thread.running is True


def test_the_tick_delta_is_the_configured_fixed_step(ai_thread):
    """A fixed timestep is what makes the production AI reproducible."""
    _thread, _logic, state, _ai = ai_thread(tick_rate=50)
    _wait_for(lambda: len(state["deltas"]) >= 3, what="three ticks")
    assert set(state["deltas"][:3]) == {1.0 / 50}


def test_stop_ends_the_thread(ai_thread):
    thread, _logic, state, _ai = ai_thread()
    _wait_for(lambda: state["ticks"] > 0, what="the first tick")
    thread.stop()
    thread.join(timeout=DEADLINE)
    assert thread.is_alive() is False
    assert thread.running is False


def test_no_ticks_happen_after_the_thread_has_joined(ai_thread):
    thread, _logic, state, _ai = ai_thread()
    _wait_for(lambda: state["ticks"] > 0, what="the first tick")
    thread.stop()
    thread.join(timeout=DEADLINE)
    settled = state["ticks"]
    time.sleep(0.1)
    assert state["ticks"] == settled


def test_stopping_twice_is_harmless(ai_thread):
    thread, _logic, _state, _ai = ai_thread()
    thread.stop()
    thread.stop()
    thread.join(timeout=DEADLINE)
    assert thread.is_alive() is False


def test_stopping_a_thread_that_never_started_is_harmless():
    logic = LogicThread(ThreadedGameState(), EditorState())
    try:
        thread = MonsterAIThread(logic, logic.monster_ai, threading.RLock())
        thread.stop()
        assert thread.running is False
        assert thread.is_alive() is False
    finally:
        logic.stop()


def test_a_restarted_ai_runs_on_a_fresh_thread(ai_thread):
    first, _logic1, state1, _ai1 = ai_thread()
    _wait_for(lambda: state1["ticks"] > 0, what="the first thread's tick")
    first.stop()
    first.join(timeout=DEADLINE)
    quiesced = state1["ticks"]

    second, _logic2, state2, _ai2 = ai_thread()
    _wait_for(lambda: state2["ticks"] > 0, what="the restarted thread's tick")

    assert second is not first
    assert state1["ticks"] == quiesced


def test_the_thread_is_a_daemon_so_it_cannot_hold_the_process_open(ai_thread):
    thread, _logic, _state, _ai = ai_thread()
    assert thread.daemon is True
    assert thread.name == "MonsterAIThread"


def test_every_tick_is_taken_under_the_shared_lock(ai_thread):
    lock = threading.RLock()
    thread, _logic, state, _ai = ai_thread(lock=lock)
    depths = []
    state["before_update"] = lambda: depths.append(1 if lock._is_owned() else 0)
    _wait_for(lambda: state["ticks"] > 0, what="a tick")
    assert all(depth >= 1 for depth in depths)
    thread.stop()
    thread.join(timeout=DEADLINE)


def test_holding_the_lock_keeps_the_ai_out(ai_thread):
    lock = threading.RLock()
    thread, _logic, state, _ai = ai_thread(lock=lock)
    _wait_for(lambda: state["ticks"] > 0, what="a tick")
    with lock:
        settled = state["ticks"]
        time.sleep(0.1)
        blocked = state["ticks"]
    thread.stop()
    thread.join(timeout=DEADLINE)
    assert blocked == settled


def test_an_exception_on_the_ai_thread_is_logged_and_the_ai_carries_on(ai_thread, monkeypatch):
    """One injected failure must not stop the real AI thread."""
    import engine.monster_ai as monster_ai_module

    logged = []
    monkeypatch.setattr(monster_ai_module, "debug_log",
                        lambda category, message: logged.append((category, message)))
    thread, _logic, state, _ai = ai_thread()
    state["raise_on_tick"] = 3
    _wait_for(lambda: state["ticks"] >= 6, what="ticks after the failing one")
    assert thread.is_alive()
    assert any("deliberate failure on tick 3" in message
               for _category, message in logged), logged


def test_a_stop_straight_after_start_is_not_lost(ai_thread, monkeypatch):
    real_run = MonsterAIThread.run

    def late_run(self):
        time.sleep(0.05)
        real_run(self)

    monkeypatch.setattr(MonsterAIThread, "run", late_run)
    thread, _logic, _state, _ai = ai_thread()
    thread.stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_stop_wakes_a_sleeping_thread_promptly(ai_thread):
    thread, _logic, state, _ai = ai_thread(tick_rate=1)
    time.sleep(0.05)
    started = time.perf_counter()
    thread.stop()
    thread.join(timeout=DEADLINE)
    assert not thread.is_alive()
    assert time.perf_counter() - started < 0.5