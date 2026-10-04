"""The logic thread against the UI thread that starts and stops play.

``QtGameView`` enters and exits Play Mode through ``LogicSession`` while the
logic thread is looping.  The session owns the mode bit and builds/tears down
movers, doors, collision caches, the spatial grid, the Prop session, and the
monster thread under the tick lock, so no tick observes a half-built session
(or a half-torn-down one).

These tests use real threads, bounded by explicit timeouts, and make the
interleaving deterministic by starting a frame from inside the transition.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import PlayerStart                     # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

DEADLINE = 5.0


@pytest.fixture
def logic():
    state = EditorState()
    state.brushes = [box_brush("floor", (0, -16, 0), (512, 32, 512))]
    state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0))]
    thread = LogicThread(ThreadedGameState(), state)
    thread.player_runtime.player = Player(0.0, 0.0)
    yield thread
    thread.session_runtime.apply_play_mode(False)
    thread.stop()


def test_play_mode_state_belongs_to_logic_session(logic):
    assert not hasattr(logic, "play_mode")
    assert logic.session_runtime.play_mode is False

    logic.session_runtime.apply_play_mode(True)
    try:
        assert logic.session_runtime.play_mode is True
    finally:
        logic.session_runtime.apply_play_mode(False)



def _frame_from_inside(logic, monkeypatch, hook_owner, hook_name):
    """Arrange for a frame to be attempted on another thread mid-transition.

    Returns ``(seen, finished)``: what each play-mode tick observed, and an
    Event set once that frame has run.  The helper is given half a second to
    finish while the transition is still in progress; if ticks are properly
    excluded it cannot, and it runs as soon as the transition ends instead.
    """
    seen = []
    finished = threading.Event()
    monkeypatch.setattr(logic.render_runtime, "prepare_render_state", lambda: None)
    monkeypatch.setattr(
        logic, "_tick_play_mode",
        lambda delta: seen.append((logic.session_runtime.spatial_grid is not None,
                                   logic.prop_runtime is not None)))
    real_hook = getattr(hook_owner, hook_name)

    def hook_with_concurrent_frame(*args, **kwargs):
        helper = threading.Thread(
            target=lambda: (logic._step_frame(logic.TICK_DURATION), finished.set()),
            daemon=True)
        helper.start()
        helper.join(timeout=0.5)
        return real_hook(*args, **kwargs)

    monkeypatch.setattr(hook_owner, hook_name, hook_with_concurrent_frame)
    return seen, finished


def test_no_tick_runs_against_a_half_built_session(logic, monkeypatch):
    # _init_doors runs early in entering play: the flag is already set, the
    # spatial grid and the Prop session do not exist yet.
    seen, finished = _frame_from_inside(logic, monkeypatch, logic.mover_runtime, "_init_doors")

    logic.session_runtime.apply_play_mode(True)

    assert finished.wait(DEADLINE), "the concurrent frame never ran"
    assert seen == [(True, True)], (
        "a play-mode tick ran mid-transition and saw (spatial grid, props) = %s"
        % (seen,))


def test_teardown_waits_for_the_tick_in_progress(logic, monkeypatch):
    """Leaving play flips the flag first, so no *new* play tick starts; the
    hazard is the one already running, which teardown used to pull the Prop
    session and the spatial grid out from under."""
    logic.session_runtime.apply_play_mode(True)
    monkeypatch.setattr(logic.render_runtime, "prepare_render_state", lambda: None)
    events = []
    entered, release = threading.Event(), threading.Event()

    def long_tick(delta):
        entered.set()
        release.wait(DEADLINE)
        events.append(("tick", logic.prop_runtime is not None,
                       logic.session_runtime.spatial_grid is not None))

    real_reset_doors = logic.mover_runtime._reset_doors

    def reset_doors():
        events.append(("teardown",))
        return real_reset_doors()

    monkeypatch.setattr(logic, "_tick_play_mode", long_tick)
    monkeypatch.setattr(logic.mover_runtime, "_reset_doors", reset_doors)

    frame = threading.Thread(
        target=lambda: logic._step_frame(logic.TICK_DURATION), daemon=True)
    frame.start()
    assert entered.wait(DEADLINE), "the play tick never started"
    stopper = threading.Thread(target=lambda: logic.session_runtime.apply_play_mode(False),
                               daemon=True)
    stopper.start()
    time.sleep(0.3)
    torn_down_early = ("teardown",) in events
    release.set()
    frame.join(DEADLINE)
    stopper.join(DEADLINE)

    assert not torn_down_early, "teardown began while a play tick was running"
    assert events == [("tick", True, True), ("teardown",)], events


def test_leaving_play_waits_for_the_monster_thread(logic):
    logic.session_runtime.apply_play_mode(True)
    ai_thread = logic.session_runtime.monster_ai_thread
    assert ai_thread is not None and ai_thread.is_alive()

    logic.session_runtime.apply_play_mode(False)

    assert not ai_thread.is_alive(), (
        "the monster thread was still running after play mode ended; its next "
        "update would read the spatial grid that was just released")


def test_a_stop_straight_after_start_is_not_lost(logic, monkeypatch):
    """``running`` used to be set inside ``run()``, after a racing ``stop()``."""
    real_run = LogicThread.run

    def late_run(self):
        time.sleep(0.05)
        real_run(self)

    monkeypatch.setattr(LogicThread, "run", late_run)
    logic.start()
    logic.stop()
    logic.join(timeout=1.0)
    alive = logic.is_alive()
    logic.stop()
    assert not alive, "a stop issued right after start was lost"
