"""World pause: owners hold it, the play tick and the monster AI honour it.

``LogicSession.set_world_paused(owner, paused)`` freezes the play-mode world
while any owner holds a request -- a game's modal screen, the actor picker, a
pause menu -- and keeps plugins ticking so a game's menus still work. These
tests pin the three halves of that contract:

* requests are per owner, so releasing one never unpauses another, and a play
  session never inherits a request from the one before it;
* a paused play tick moves nothing and drains look/fire input, while plugins
  still tick with the use key;
* the monster AI thread idles while paused and does not fast-forward after.

Every "nothing happened" assertion has an unpaused control beside it, so a
test cannot pass because the thing it watches would not have moved anyway.
"""

import copy
import threading
import time

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import PlayerStart                     # noqa: E402
from engine.logic_player import KEY_W
from engine.logic_thread import LogicThread        # noqa: E402
from engine.monster_ai import MonsterAIThread             # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


@pytest.fixture
def logic():
    state = EditorState()
    state.brushes = [box_brush("floor", (0, -16, 0), (2048, 32, 2048))]
    state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0))]
    thread = LogicThread(ThreadedGameState(), state)
    thread.player = Player(0.0, 0.0)
    thread.player.pos.y = 40.0
    yield thread
    thread.session_runtime.apply_play_mode(False)
    thread.stop()


@pytest.fixture
def playing(logic):
    logic.session_runtime.apply_play_mode(True)
    # The AI thread is not under test here; keep the tick single-threaded.
    logic.session_runtime.stop_monster_ai()
    return logic


class _RecordingPlugins:
    def __init__(self):
        self.ticks = []

    def wants_tick(self):
        return True

    def tick(self, logic, **kwargs):
        self.ticks.append(kwargs)


def _ticks(logic, n=10):
    for _ in range(n):
        logic._tick_play_mode(TICK)


# ---------------------------------------------------------------------------
# Owners
# ---------------------------------------------------------------------------

def test_the_world_is_paused_while_any_owner_holds_a_request(logic):
    assert logic.session_runtime.world_paused is False
    logic.session_runtime.set_world_paused("menu", True)
    logic.session_runtime.set_world_paused("picker", True)
    assert logic.session_runtime.world_paused is True
    assert logic.session_runtime.world_pause_owners() == {"menu", "picker"}
    logic.session_runtime.set_world_paused("menu", False)
    assert logic.session_runtime.world_paused is True, "releasing one owner unpaused the other"
    logic.session_runtime.set_world_paused("picker", False)
    assert logic.session_runtime.world_paused is False


def test_releasing_twice_or_releasing_a_stranger_is_harmless(logic):
    logic.session_runtime.set_world_paused("menu", True)
    logic.session_runtime.set_world_paused("stranger", False)
    logic.session_runtime.set_world_paused("menu", False)
    logic.session_runtime.set_world_paused("menu", False)
    assert logic.session_runtime.world_pause_owners() == frozenset()


def test_holding_twice_is_still_one_request(logic):
    logic.session_runtime.set_world_paused("menu", True)
    logic.session_runtime.set_world_paused("menu", True)
    logic.session_runtime.set_world_paused("menu", False)
    assert logic.session_runtime.world_paused is False


@pytest.mark.parametrize("entering", [True, False])
def test_a_play_mode_change_drops_every_request(logic, entering):
    if not entering:
        logic.session_runtime.apply_play_mode(True)
    logic.session_runtime.set_world_paused("leftover", True)
    logic.session_runtime.apply_play_mode(entering)
    assert logic.session_runtime.world_paused is False


def test_requests_from_many_threads_are_not_lost(logic):
    def hold(i):
        for _ in range(200):
            logic.session_runtime.set_world_paused(("t", i), True)
            logic.session_runtime.set_world_paused(("t", i), False)
        logic.session_runtime.set_world_paused(("t", i), True)

    threads = [threading.Thread(target=hold, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert logic.session_runtime.world_pause_owners() == {("t", i) for i in range(8)}


# ---------------------------------------------------------------------------
# The play tick
# ---------------------------------------------------------------------------

def test_an_unpaused_tick_moves_the_player(playing):
    """The control: holding W does move the player in this world."""
    start = glm.vec3(playing.player.pos)
    playing.game_state.set_keys({KEY_W})
    _ticks(playing, 20)
    assert glm.distance(playing.player.pos, start) > 1.0


def test_a_paused_tick_does_not_move_the_player(playing):
    start = glm.vec3(playing.player.pos)
    playing.session_runtime.set_world_paused("menu", True)
    playing.game_state.set_keys({KEY_W})
    _ticks(playing, 20)
    assert glm.distance(playing.player.pos, start) == 0.0


def test_look_and_fire_over_a_paused_world_are_discarded(playing):
    angle = playing.player.angle
    events_before = len(playing.combat_runtime.get_recent_noise_events())
    playing.session_runtime.set_world_paused("menu", True)
    playing.game_state.set_mouse_delta(80.0, 30.0)
    playing.game_state.queue_shot()
    _ticks(playing, 1)
    assert playing.player.angle == angle
    assert playing.combat_runtime.muzzle_flash_active is False

    playing.session_runtime.set_world_paused("menu", False)
    _ticks(playing, 1)
    assert playing.player.angle == angle, "look input queued over a menu landed on resume"
    assert playing.combat_runtime.muzzle_flash_active is False
    assert len(playing.combat_runtime.get_recent_noise_events()) == events_before


def test_a_paused_tick_leaves_world_runtime_state_unchanged(playing):
    before = {
        "player_pos": tuple(playing.player.pos),
        "player_angle": playing.player.angle,
        "movers": copy.deepcopy(playing.mover_runtime.mover_states),
        "doors": copy.deepcopy(playing.mover_runtime.door_states),
        "timers": copy.deepcopy(playing.timing_runtime.timer_states),
        "fades": copy.deepcopy(playing.timing_runtime.light_fade_states),
        "projectiles": playing.combat_runtime.projectile_positions.copy(),
        "noise": copy.deepcopy(playing.combat_runtime._gunfire_events),
    }

    playing.session_runtime.set_world_paused("menu", True)
    _ticks(playing, 5)

    assert tuple(playing.player.pos) == before["player_pos"]
    assert playing.player.angle == before["player_angle"]
    assert playing.mover_runtime.mover_states == before["movers"]
    assert playing.mover_runtime.door_states == before["doors"]
    assert playing.timing_runtime.timer_states == before["timers"]
    assert playing.timing_runtime.light_fade_states == before["fades"]
    assert np.array_equal(playing.combat_runtime.projectile_positions, before["projectiles"])
    assert playing.combat_runtime._gunfire_events == before["noise"]


def test_plugins_still_tick_over_a_paused_world(playing):
    plugins = _RecordingPlugins()
    playing.plugins = plugins
    playing.session_runtime.set_world_paused("menu", True)
    playing.game_state.set_use_key_pressed()
    _ticks(playing, 2)
    assert len(plugins.ticks) == 2
    assert plugins.ticks[0]["use_pressed"] is True
    assert plugins.ticks[0]["delta"] == pytest.approx(TICK)
    assert plugins.ticks[1]["use_pressed"] is False


@pytest.mark.parametrize("state", ["cutscene_runtime.state", "player_dead", "level_complete_ui"])
def test_plugins_over_a_paused_world_tick_only_when_an_unpaused_tick_would(playing, state):
    """A cinematic, a death or the level-complete screen returns before the
    plugin step either way; pausing on top of one does not start plugins."""
    plugins = _RecordingPlugins()
    playing.plugins = plugins
    if state == "cutscene_runtime.state":
        playing.cutscene_runtime.state = {"active": True}
    elif state == "player_dead":
        playing.player_dead = True
    else:
        playing.interaction_runtime.level_complete_ui = {"active": True}
    playing.session_runtime.set_world_paused("menu", True)
    playing.game_state.set_use_key_pressed()
    _ticks(playing, 2)
    assert plugins.ticks == []
    assert playing.game_state.consume_use_key() is False      # still drained


def test_a_paused_tick_reports_a_hud_prompt_as_consuming_the_use_key(playing):
    plugins = _RecordingPlugins()
    playing.plugins = plugins
    playing.interaction_runtime.current_hud_message = "Press E to open"
    playing.session_runtime.set_world_paused("menu", True)
    _ticks(playing, 1)
    assert plugins.ticks[0]["interaction_consumed"] is True


# ---------------------------------------------------------------------------
# The monster AI thread
# ---------------------------------------------------------------------------

class _CountingAI:
    def __init__(self):
        self.updates = 0

    def update(self, delta):
        self.updates += 1


class _Host:
    class _Session:
        world_paused = False
    session_runtime = _Session()


def _wait_for(predicate, timeout=2.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_monster_ai_thread_idles_while_the_world_is_paused():
    host, ai = _Host(), _CountingAI()
    thread = MonsterAIThread(host, ai, threading.Lock(), tick_rate=100)
    thread.start()
    try:
        assert _wait_for(lambda: ai.updates > 3), "the control: the AI runs"
        host.session_runtime.world_paused = True
        time.sleep(0.05)                     # let an in-flight frame finish
        frozen = ai.updates
        time.sleep(0.3)
        assert ai.updates == frozen
        host.session_runtime.world_paused = False
        assert _wait_for(lambda: ai.updates > frozen), "the AI did not resume"
    finally:
        thread.stop()
        thread.join(timeout=2.0)


def test_the_monster_ai_does_not_fast_forward_after_a_pause():
    """0.5 s paused at 100 Hz would be ~50 catch-up updates in one burst."""
    host, ai = _Host(), _CountingAI()
    host.session_runtime.world_paused = True
    thread = MonsterAIThread(host, ai, threading.Lock(), tick_rate=100)
    thread.start()
    try:
        time.sleep(0.5)
        assert ai.updates == 0
        host.session_runtime.world_paused = False
        time.sleep(0.03)
        assert ai.updates < 15
    finally:
        thread.stop()
        thread.join(timeout=2.0)
