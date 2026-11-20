"""Who holds which lock when the UI and MonsterAI threads touch the session.

The engine's lock order is ``_tick_lock`` then ``monster_lock``. The MonsterAI
thread runs on ``monster_lock`` alone, so anything the UI thread does to state
that thread reads -- the spatial grid and physics a collision toggle rebuilds,
the ``monster_states`` a save reads and a load replaces -- has to hold both,
in that order; and anything the AI thread hands outwards (plugin events) must
not run listeners under ``monster_lock``, where a listener taking the tick
lock deadlocks against the logic thread.

Ticks are driven by hand; the AI thread is only started where a test needs
it to exist.
"""

import threading

import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState          # noqa: E402
from editor.things import Monster, PlayerStart       # noqa: E402
from engine.logic_thread import LogicThread          # noqa: E402
from engine.player import Player                     # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import make_thing, room    # noqa: E402

pytestmark = pytest.mark.qt

DEADLINE = 5.0
TICK = 1.0 / 60.0


class _Plugins:
    """The plugin manager surface the session and tick call, recording emits."""

    def __init__(self, logic):
        self.logic = logic
        self.services = {}
        self.emitted = []
        self.play_started_with_ai = []

    def emit(self, event, logic=None, **data):
        session = self.logic.session_runtime
        self.emitted.append((event, threading.get_ident(),
                             self.logic._tick_lock._is_owned(),
                             session.monster_lock._is_owned()))

    def dispatch_play_start(self, logic):
        self.play_started_with_ai.append(logic.session_runtime.monster_ai_thread)

    def dispatch_play_stop(self, logic):
        pass

    def wants_tick(self):
        return False

    def has_listeners(self, event):
        return False


@pytest.fixture
def logic():
    threads = []

    def _build(things=(), threaded_ai=False):
        state = EditorState()
        state.brushes = list(room())
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state, threaded_ai=threaded_ai)
        thread.plugins = _Plugins(thread)
        thread.player_runtime.player = Player(0.0, 0.0)
        threads.append(thread)
        return thread

    yield _build

    for thread in threads:
        thread.session_runtime.stop_monster_ai()


# ---------------------------------------------------------------------------
# S7: player events raised on the AI thread reach plugins on the tick
# ---------------------------------------------------------------------------

def _damage_from_another_thread(thread, damage):
    session = thread.session_runtime

    def ai_update():
        with session.monster_lock:       # as MonsterAIThread runs an update
            thread.trigger_runtime._apply_player_damage(damage)

    ai = threading.Thread(target=ai_update, daemon=True)
    ai.start()
    ai.join(timeout=DEADLINE)
    assert not ai.is_alive()


def test_player_damage_from_the_ai_thread_is_emitted_on_the_next_tick(logic):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.plugins.emitted.clear()
        _damage_from_another_thread(thread, 30)
        assert thread.player_runtime.player_health == 70, "the damage itself is applied at once"
        assert [e for e in thread.plugins.emitted if e[0] == "player_damage"] == [], (
            "player_damage ran its listeners on the AI thread, under the monster lock")

        with thread._tick_lock:
            thread._tick(TICK)

        damage = [e for e in thread.plugins.emitted if e[0] == "player_damage"]
        assert len(damage) == 1
        _event, ident, tick_owned, monster_owned = damage[0]
        assert ident == threading.get_ident()
        assert tick_owned and not monster_owned
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_player_death_from_the_ai_thread_follows_its_damage_in_order(logic):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.plugins.emitted.clear()
        _damage_from_another_thread(thread, 500)
        with thread._tick_lock:
            thread._tick(TICK)
        names = [e[0] for e in thread.plugins.emitted if e[0].startswith("player_")]
        assert names == ["player_damage", "player_death"]
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_damage_on_the_tick_is_still_emitted_in_place(logic):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.plugins.emitted.clear()
        with thread._tick_lock:
            thread.trigger_runtime._apply_player_damage(10)
        assert [e[0] for e in thread.plugins.emitted] == ["player_damage"]
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_events_queued_by_a_finished_session_are_not_delivered_to_the_next(logic):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    _damage_from_another_thread(thread, 10)
    thread.session_runtime.apply_play_mode(False)
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.plugins.emitted.clear()
        with thread._tick_lock:
            thread._tick(TICK)
        assert not [e for e in thread.plugins.emitted if e[0] == "player_damage"]
    finally:
        thread.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# S6: save and load hold both locks; the AI thread starts last
# ---------------------------------------------------------------------------

def test_save_builds_its_snapshot_under_the_tick_lock_then_the_monster_lock(
        logic, monkeypatch, tmp_path):
    from engine import savegame
    thread = logic(things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.session_runtime.apply_play_mode(True)
    try:
        held = []
        real = savegame.build_snapshot

        def recording(logic_, **kwargs):
            held.append((thread._tick_lock._is_owned(),
                         thread.session_runtime.monster_lock._is_owned()))
            return real(logic_, **kwargs)

        monkeypatch.setattr(savegame, "build_snapshot", recording)
        ok, message = thread.session_runtime.save_session(str(tmp_path / "s.fiosave"))
        assert ok, message
        assert held == [(True, True)]
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_load_restores_under_both_locks(logic, monkeypatch, tmp_path):
    from engine import savegame
    thread = logic(things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.session_runtime.apply_play_mode(True)
    try:
        path = str(tmp_path / "s.fiosave")
        ok, message = thread.session_runtime.save_session(path)
        assert ok, message
        held = []
        real = savegame.restore_auto

        def recording(logic_, data, **kwargs):
            held.append((thread._tick_lock._is_owned(),
                         thread.session_runtime.monster_lock._is_owned()))
            return real(logic_, data, **kwargs)

        monkeypatch.setattr(savegame, "restore_auto", recording)
        thread._clock_resync = False
        ok, message = thread.session_runtime.load_session(path)
        assert ok, message
        assert held == [(True, True)]
        assert thread._clock_resync, "a load did not resync the loop's clock"
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_a_snapshot_that_cannot_read_monster_state_fails_the_save(logic, tmp_path):
    """It used to be swallowed into an empty monster_states: a save with
    every monster's state missing, reported as a success."""
    thread = logic(things=[make_thing(Monster, "grunt", (0, 96, 0))])
    thread.session_runtime.apply_play_mode(True)
    try:
        class Unreadable(dict):
            def items(self):
                raise RuntimeError("dictionary changed size during iteration")

        thread.monster_ai.monster_states = Unreadable({1: {}})
        path = tmp_path / "s.fiosave"
        ok, message = thread.session_runtime.save_session(str(path))
        assert not ok and "dictionary changed size" in message
        assert not path.exists()
    finally:
        thread.monster_ai.monster_states = {}
        thread.session_runtime.apply_play_mode(False)


def test_the_monster_ai_thread_starts_after_the_plugins_play_start(logic):
    thread = logic(things=[make_thing(Monster, "grunt", (0, 96, 0))], threaded_ai=True)
    thread.session_runtime.apply_play_mode(True)
    try:
        assert thread.plugins.play_started_with_ai == [None], (
            "the AI thread was already running while plugins set the session up")
        assert thread.session_runtime.monster_ai_thread is not None
        assert thread.session_runtime.monster_ai_thread.is_alive()
    finally:
        thread.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# S13: saving with no plugin system
# ---------------------------------------------------------------------------

def test_save_works_with_the_plugin_system_off(logic, tmp_path):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        thread.plugins = None            # FIO_NO_PLUGINS=1, or a failed load
        ok, message = thread.session_runtime.save_session(str(tmp_path / "s.fiosave"))
        assert ok, message
        assert (tmp_path / "s.fiosave").exists()
    finally:
        thread.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# S5: the collision toggle rebuilds the grid and physics under both locks
# ---------------------------------------------------------------------------

def test_toggling_model_collision_holds_the_tick_then_the_monster_lock(logic, monkeypatch):
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        held = []
        grid = thread.session_runtime.spatial_grid
        real = grid.populate

        def recording(brushes):
            held.append((thread._tick_lock._is_owned(),
                         thread.session_runtime.monster_lock._is_owned()))
            return real(brushes)

        monkeypatch.setattr(grid, "populate", recording)
        thread.collision_runtime.toggle_model_collision(False)
        thread.collision_runtime.toggle_model_collision(True)
        assert held == [(True, True), (True, True)]
    finally:
        thread.session_runtime.apply_play_mode(False)


def test_the_collision_overlay_never_rebuilds_the_session_from_paint(
        logic, main_window, monkeypatch):
    """With the overlay on and no model collision in the level, every paint
    used to call toggle_model_collision: a grid repopulate and a physics
    rebuild on the UI thread, unlocked, 60 times a second."""
    thread = logic()
    thread.session_runtime.apply_play_mode(True)
    try:
        toggles, builds = [], []
        monkeypatch.setattr(thread.collision_runtime, "toggle_model_collision",
                            lambda *a: toggles.append(a))
        real_build = thread.collision_runtime.build_model_collision_brushes

        def counting_build():
            builds.append(thread._tick_lock._is_owned())
            return real_build()

        monkeypatch.setattr(thread.collision_runtime, "build_model_collision_brushes",
                            counting_build)
        view = main_window.view_3d       # the real view, drawing this session
        monkeypatch.setattr(view, "logic_thread", thread)
        monkeypatch.setattr(view, "play_mode", True)
        for _ in range(60):
            assert view._collision_vis_brushes() == []
        assert toggles == [], "paint changed the logic thread's collision state"
        assert builds == [True], "the overlay's set was built %d times" % len(builds)

        thread.editor_state.mark_world_changed()
        view._collision_vis_brushes()
        assert len(builds) == 2, "an edit did not refresh the overlay's set"
    finally:
        thread.session_runtime.apply_play_mode(False)


# ---------------------------------------------------------------------------
# Spawn I/O is delivered during setup in every Play session, not just the first
# ---------------------------------------------------------------------------

def test_spawn_outputs_are_delivered_in_place_in_every_session(logic, monkeypatch):
    start = make_thing(PlayerStart, "spawn", (0, 40, 0))
    thread = logic(things=[start])
    io = thread.io_manager
    delivered = []
    real = io._get_connections

    def recording(entity):
        if entity is start:
            delivered.append(len(delivered))
        return real(entity)

    monkeypatch.setattr(io, "_get_connections", recording)

    for session in range(3):
        thread.session_runtime.apply_play_mode(True)
        assert len(delivered) == session + 1, (
            "OnPlayerSpawn was not delivered during setup of session %d" % (session + 1))
        assert not io._foreign_outputs
        # The session's ticks run on another thread (the logic loop).
        ticker = threading.Thread(target=lambda: io.update(TICK), daemon=True)
        ticker.start()
        ticker.join(timeout=DEADLINE)
        thread.session_runtime.apply_play_mode(False)
