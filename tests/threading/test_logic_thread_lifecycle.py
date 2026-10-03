"""``LogicThread``: start, stop, tick, and the state a play session owns.

The logic thread is Fio's simulation loop and the owner of the shared
editor/runtime state — it reads the editor's brush and thing lists live and
writes a render snapshot for the Qt view.  What has to be true of it is
therefore less about any one tick and more about lifetime: nothing may be left
running after ``stop()``, and entering and leaving play mode must leave the
editor's world exactly as the mapper authored it.

Ticks are driven by calling ``_tick(delta)`` directly with a fixed step rather
than by starting the thread, so results do not depend on scheduling.  The few
tests that must start the real thread are marked ``slow`` and bounded by an
explicit deadline.
"""

import threading
import time

import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")
from PyQt5.QtCore import QCoreApplication, QObject, pyqtSignal, pyqtSlot  # noqa: E402

from editor.editor_state import EditorState          # noqa: E402
from editor.things import Light, Monster, PlayerStart  # noqa: E402
from engine.logic_thread import LogicThread          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt

DEADLINE = 5.0
TICK = 1.0 / 60.0


class _FaultBridge(QObject):
    fault = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.thread_ids = []
        self.fault.connect(self._on_fault)

    @pyqtSlot(str)
    def _on_fault(self, message):
        self.calls.append(message)
        self.thread_ids.append(threading.get_ident())


@pytest.fixture
def logic():
    """A LogicThread over a small world, guaranteed to be stopped afterwards."""
    threads = []

    def _build(brushes=(), things=()):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state)
        threads.append(thread)
        return thread

    yield _build

    for thread in threads:
        thread.stop()
        if thread.is_alive():
            thread.join(timeout=DEADLINE)
        ai_thread = thread.monster_ai_thread
        if ai_thread is not None:
            ai_thread.stop()
            ai_thread.join(timeout=DEADLINE)


def _wait_for(predicate, timeout=DEADLINE, what="condition"):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if predicate():
            return True
        time.sleep(0.005)
    pytest.fail("timed out after %.1fs waiting for %s" % (timeout, what))


# ---------------------------------------------------------------------------
# The world is shared, not copied
# ---------------------------------------------------------------------------

def test_the_logic_thread_reads_the_editor_state_live(logic):
    """Fio's whole premise: one world, no serialisation between the two."""
    state_brushes = [box_brush("wall")]
    thread = logic(brushes=state_brushes)
    assert thread.brushes is thread.editor_state.brushes, (
        "the logic thread copied the brush list; an edit in the editor would "
        "not reach the running game")

    thread.editor_state.brushes.append(box_brush("added_later"))
    assert [b["name"] for b in thread.brushes] == ["wall", "added_later"], (
        "a brush added in the editor did not appear in the logic thread's "
        "view: %s" % ([b["name"] for b in thread.brushes],))


def test_things_are_shared_the_same_way(logic):
    thread = logic(things=[make_thing(Light, "lamp")])
    assert thread.things is thread.editor_state.things
    thread.editor_state.things.append(make_thing(Light, "lamp2"))
    assert [t.name for t in thread.things] == ["lamp", "lamp2"]


# ---------------------------------------------------------------------------
# Entering and leaving play mode
# ---------------------------------------------------------------------------

def test_entering_play_mode_builds_the_spatial_grid_from_the_live_world(logic):
    brushes = room(size=1024.0)
    thread = logic(brushes=brushes)

    thread.set_play_mode(True)

    assert thread._spatial_grid is not None, "no spatial grid was built"
    filed = {id(b) for bucket in thread._spatial_grid.cells.values() for b in bucket}
    missing = [b["name"] for b in brushes if id(b) not in filed]
    assert not missing, "brushes missing from the play-mode grid: %s" % (missing,)
    assert thread.monster_ai._grid is thread._spatial_grid, (
        "MonsterAI was not handed the grid the logic thread built")


def test_leaving_play_mode_releases_the_spatial_grid(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    grid = thread._spatial_grid

    thread.set_play_mode(False)

    assert thread._spatial_grid is None, "the grid outlived the play session"
    assert grid.cells == {}, "the released grid still holds brush references"
    assert thread.monster_ai._grid is None, (
        "MonsterAI still holds the grid from the finished session")


def test_entering_play_mode_starts_the_monster_ai_thread(logic):
    thread = logic(brushes=room(), things=[make_thing(Monster, "grunt", (0, 96, 0))])

    thread.set_play_mode(True)
    try:
        assert thread.monster_ai_thread is not None
        assert thread.monster_ai_thread.is_alive(), "the AI thread did not start"
    finally:
        thread.set_play_mode(False)


def test_leaving_play_mode_stops_the_monster_ai_thread(logic):
    thread = logic(brushes=room(), things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.set_play_mode(True)
    ai_thread = thread.monster_ai_thread

    thread.set_play_mode(False)

    assert thread.monster_ai_thread is None, (
        "the logic thread still holds a reference to the finished AI thread")
    _wait_for(lambda: not ai_thread.is_alive(),
              what="the monster AI thread to exit after leaving play mode")


def test_restarting_play_mode_does_not_leave_the_old_ai_thread_running(logic):
    thread = logic(brushes=room(), things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.set_play_mode(True)
    first = thread.monster_ai_thread
    thread.set_play_mode(False)
    thread.set_play_mode(True)
    second = thread.monster_ai_thread
    try:
        assert second is not first, "play mode reused the previous AI thread"
        _wait_for(lambda: not first.is_alive(),
                  what="the first AI thread to exit")
        assert second.is_alive()
    finally:
        thread.set_play_mode(False)


def test_stop_ends_the_monster_ai_thread_as_well(logic):
    thread = logic(brushes=room(), things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.set_play_mode(True)
    ai_thread = thread.monster_ai_thread

    thread.stop()

    assert thread.running is False
    _wait_for(lambda: not ai_thread.is_alive(),
              what="the AI thread to exit when the logic thread stopped")


# ---------------------------------------------------------------------------
# Play mode must not corrupt the authored world
# ---------------------------------------------------------------------------

def test_play_mode_resets_player_state_every_time(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    thread.player_health = 3
    thread.player_dead = True
    thread.god_mode = True
    thread.set_play_mode(False)

    thread.set_play_mode(True)
    try:
        assert thread.player_health == 100, (
            "player health carried over from the previous session (%d)"
            % thread.player_health)
        assert thread.player_dead is False
        assert thread.god_mode is False, "a cheat leaked into the next session"
    finally:
        thread.set_play_mode(False)


def test_leaving_play_mode_clears_the_session_only_state(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    thread.collected_keys.add("red")
    thread.current_hud_message = "you need the red key"
    thread.bullet_marks.append({"pos": None, "time": 0.0})

    thread.set_play_mode(False)

    assert thread.collected_keys == set(), \
        "collected keys survived into editor mode: %s" % (thread.collected_keys,)
    assert thread.current_hud_message == ""
    assert thread.bullet_marks == []


def test_a_monsters_runtime_state_is_reset_between_sessions(logic):
    monster = make_thing(Monster, "grunt", (0, 96, 0), awake=True)
    thread = logic(brushes=room(), things=[monster])

    thread.set_play_mode(True)
    monster.properties["awake"] = True
    monster.properties["dead"] = True
    thread.set_play_mode(False)

    assert monster.properties.get("dead") is True, (
        "leaving play mode with clear_dead=False should leave 'dead' alone so "
        "the editor still shows what happened")
    thread.set_play_mode(True)
    try:
        assert "dead" not in monster.properties, (
            "a monster killed in the last session is still marked dead=%r at "
            "the start of the next one" % monster.properties.get("dead"))
        assert monster.properties["awake"] is False, (
            "a wake_on_sight monster must start each session asleep, not %r"
            % monster.properties["awake"])
    finally:
        thread.set_play_mode(False)


def test_leaving_play_mode_removes_the_model_collision_pseudo_brushes(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    thread.set_play_mode(False)
    assert thread._model_collision_brushes == [], (
        "model collision brushes built for the session were left behind")
    # The session's collision set is rebuilt when Play starts; kept past
    # Stop it pinned the session's brushes (see test_final_audit).
    assert thread._collision_brushes_cache == []
    thread.set_play_mode(True)
    assert thread._collision_brushes_cache == thread.brushes


def test_the_render_projection_survives_the_play_mode_round_trip(logic):
    """The projection is not a play-session artefact.

    The cull buffers it replaced were built on entering play and released on
    leaving, because they only served the play-mode fast path.  The projection
    serves both modes, so it is not torn down -- but it must still describe the
    world correctly on the other side of the switch.
    """
    brushes = room()
    thread = logic(brushes=brushes)
    thread.set_play_mode(True)
    thread._prepare_render_state()
    table = thread._render_table
    assert table.count == len(brushes)

    thread.set_play_mode(False)
    thread._prepare_render_state()

    assert table.count == len(brushes)
    for index, brush in enumerate(brushes):
        assert table.ids[index] == brush["id"]
        assert list(table.center[index]) == pytest.approx(brush["pos"])



# ---------------------------------------------------------------------------
# Health HUD opacity
# ---------------------------------------------------------------------------

def test_health_hud_fades_in_on_spawn_then_settles_at_50_percent(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        start = thread._hud_health_fade_started
        assert start is not None
        assert thread._hud_health_fade_phase == "in"
        assert thread._hud_health_alpha == 0.0

        alpha = thread._update_hud_health_alpha(start + 0.75)
        assert alpha == pytest.approx(0.5)

        alpha = thread._update_hud_health_alpha(start + 1.5)
        assert alpha == pytest.approx(1.0)
        assert thread._hud_health_fade_phase == "out"

        alpha = thread._update_hud_health_alpha(start + 3.5)
        assert alpha == pytest.approx(0.75)

        alpha = thread._update_hud_health_alpha(start + 5.5)
        assert alpha == pytest.approx(0.5)
        assert thread._hud_health_fade_phase == "idle"
    finally:
        thread.set_play_mode(False)


def test_disabling_health_hud_fade_keeps_full_alpha_after_damage(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        thread.set_hud_fade_enabled(False)
        thread.player_health = 25

        # Disabled fade mode clears the timing marker deliberately;
        # sampling a timestamp is irrelevant because the disabled branch is
        # immediately forced to full opacity.
        alpha = thread._update_hud_health_alpha(0.0)

        assert alpha == pytest.approx(1.0)
        assert thread._hud_health_fade_phase == "idle"
        assert thread._hud_health_fade_started is None
    finally:
        thread.set_play_mode(False)


def test_health_change_uses_fast_fade_in_then_slow_fade_out(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        start = thread._hud_health_fade_started
        assert start is not None
        thread._update_hud_health_alpha(start + 5.5)
        assert thread._hud_health_alpha == pytest.approx(0.5)

        change = start + 10.0
        thread.player_health = 75

        alpha = thread._update_hud_health_alpha(change)
        assert alpha == pytest.approx(0.5)
        assert thread._hud_health_fade_phase == "in"

        alpha = thread._update_hud_health_alpha(change + 0.75)
        assert alpha == pytest.approx(0.75)

        alpha = thread._update_hud_health_alpha(change + 1.5)
        assert alpha == pytest.approx(1.0)
        assert thread._hud_health_fade_phase == "out"

        alpha = thread._update_hud_health_alpha(change + 3.5)
        assert alpha == pytest.approx(0.75)

        alpha = thread._update_hud_health_alpha(change + 5.5)
        assert alpha == pytest.approx(0.5)
        assert thread._hud_health_fade_phase == "idle"
    finally:
        thread.set_play_mode(False)


def test_further_health_changes_restart_the_fast_fade_from_current_opacity(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        start = thread._hud_health_fade_started
        assert start is not None
        thread._update_hud_health_alpha(start + 5.5)
        assert thread._hud_health_alpha == pytest.approx(0.5)

        first_change = start + 8.0
        thread.player_health = 90
        thread._update_hud_health_alpha(first_change)
        thread._update_hud_health_alpha(first_change + 1.5)
        assert thread._hud_health_alpha == pytest.approx(1.0)

        second_change = first_change + 3.0
        thread.player_health = 80
        alpha = thread._update_hud_health_alpha(second_change)
        assert alpha == pytest.approx(0.8125)
        assert thread._hud_health_fade_phase == "in"

        alpha = thread._update_hud_health_alpha(second_change + 1.5)
        assert alpha == pytest.approx(1.0)
        assert thread._hud_health_fade_phase == "out"
    finally:
        thread.set_play_mode(False)


# ---------------------------------------------------------------------------
# Ticking
# ---------------------------------------------------------------------------

def test_an_editor_tick_moves_the_editor_camera_and_nothing_else(logic):
    from PyQt5.QtCore import Qt
    thread = logic(brushes=room())
    before = list(thread.editor_camera.pos)
    thread.game_state.set_keys({int(Qt.Key_W)})

    thread._tick(TICK)

    assert list(thread.editor_camera.pos) != before, (
        "holding W for a tick did not move the editor camera from %s" % (before,))


def test_a_play_tick_with_no_player_does_nothing(logic):
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        thread.player = None
        thread._tick(TICK)      # must not raise
    finally:
        thread.set_play_mode(False)


def test_the_io_manager_advances_with_the_tick(logic):
    """Driven with the real Player: the tick runs its physics before the I/O
    update, so a stand-in without ``update`` would not exercise the path."""
    from engine.player import Player
    thread = logic(brushes=room())
    thread.set_play_mode(True)
    try:
        before = thread.io_manager.current_time
        thread.player = Player(0.0, 0.0)
        thread._tick(TICK)
        assert thread.io_manager.current_time > before, (
            "the I/O clock did not advance with the tick (%.4f -> %.4f); "
            "delayed connections would never fire"
            % (before, thread.io_manager.current_time))
    finally:
        thread.set_play_mode(False)


def test_a_fatal_tick_marshals_play_teardown_to_gui(logic):
    """Fatal tick handling must never tear down the play session on the worker."""
    app = QCoreApplication.instance() or QCoreApplication([])
    thread = logic(brushes=room())
    thread.play_mode = True
    bridge = _FaultBridge()
    runner_ident = []

    def _explode(delta):
        raise RuntimeError("deliberate fatal tick failure")

    def _forbidden_teardown(enabled):
        raise AssertionError("play teardown ran on the logic worker")

    thread._tick = _explode
    thread.set_gui_fault_teardown(bridge.fault.emit)
    thread._apply_play_mode = _forbidden_teardown

    def run_one_frame():
        runner_ident.append(threading.get_ident())
        thread._step_frame(thread.TICK_DURATION)

    runner = threading.Thread(target=run_one_frame, daemon=True)
    runner.start()
    runner.join(timeout=DEADLINE)

    assert not runner.is_alive(), "fatal tick frame did not finish"
    assert thread.play_mode is False, "fatal tick did not fail closed to editor state"
    assert bridge.calls == [], "GUI teardown ran before Qt processed the queued signal"

    app.processEvents()
    assert len(bridge.calls) == 1, "fatal tick did not reach the GUI thread"
    assert bridge.thread_ids[0] != runner_ident[0], (
        "fatal tick teardown callback ran on the logic worker instead of the GUI thread"
    )


def test_a_tick_that_raises_does_not_kill_the_thread(logic):
    """The loop logs and carries on; a broken entity must not freeze the game."""
    thread = logic(brushes=room())
    calls = []

    def _explode(delta):
        calls.append(delta)
        raise RuntimeError("deliberate tick failure")

    thread._tick = _explode
    thread.running = True

    runner = threading.Thread(target=thread.run, daemon=True)
    runner.start()
    try:
        _wait_for(lambda: len(calls) >= 3,
                  what="the loop to keep ticking after a raising tick")
        assert runner.is_alive(), "the logic thread died on a single bad tick"
    finally:
        thread.stop()
        runner.join(timeout=DEADLINE)
    assert not runner.is_alive(), "the logic thread did not stop"


@pytest.mark.slow
def test_the_running_thread_publishes_render_frames(logic):
    thread = logic(brushes=room())
    thread.start()
    try:
        _wait_for(thread.game_state.peek_has_new_frame,
                  what="the logic thread to publish a render frame")
        assert thread.game_state.try_swap() is True
    finally:
        thread.stop()
        thread.join(timeout=DEADLINE)
    assert not thread.is_alive()


@pytest.mark.slow
def test_stopping_a_running_thread_leaves_nothing_alive(logic):
    thread = logic(brushes=room(), things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.start()
    _wait_for(thread.game_state.peek_has_new_frame, what="the first frame")
    thread.set_play_mode(True)
    ai_thread = thread.monster_ai_thread

    thread.stop()
    thread.join(timeout=DEADLINE)

    assert not thread.is_alive(), "the logic thread is still running"
    _wait_for(lambda: not ai_thread.is_alive(), what="the AI thread to exit")
    alive = [t.name for t in threading.enumerate()
             if t.name in ("MonsterAIThread",) and t.is_alive()]
    assert not alive, "these Fio threads are still alive after stop(): %s" % (alive,)


def test_the_logic_thread_is_a_daemon(logic):
    thread = logic()
    assert thread.daemon is True, (
        "a non-daemon logic thread would keep the process alive after the "
        "editor window closed")
