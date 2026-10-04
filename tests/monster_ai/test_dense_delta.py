"""The dense pass's per-row delta and sit-out mask leave the plain tick
exactly as it was.

``MonsterAI._update_dense`` accepts one delta per row, and a mask of rows that
sit the tick out, so a Big World session fitted to an overhead camera can run
off-screen monsters less often. Every other caller passes one number, and that
must behave as it always has -- a zero-length tick included, which still
wakes, sights and shoots like the per-monster reference rather than skipping
anyone. Under a fitted view the dense pass and the per-monster path must still
agree with each other.
"""

import numpy as np
from types import SimpleNamespace
import pytest

from engine.monster_constants import MONSTER_SHOOT_INTERVAL

from .test_dense_update import TICK, _snapshot, _world

pytestmark = pytest.mark.qt


@pytest.mark.parametrize("seed", [1, 2])
def test_a_zero_tick_matches_the_per_monster_path(seed):
    dense_ai, dense_logic = _world(seed, dense=True)
    ref_ai, ref_logic = _world(seed, dense=False)
    for tick in range(int(4 * MONSTER_SHOOT_INTERVAL / TICK)):
        delta = 0.0 if tick % 3 == 0 else TICK
        dense_ai.update(delta)
        ref_ai.update(delta)
        assert _snapshot(dense_ai, dense_logic) == _snapshot(ref_ai, ref_logic), tick


def test_a_per_row_delta_equal_to_the_tick_is_the_plain_tick():
    plain_ai, plain_logic = _world(3, dense=True)
    rows_ai, rows_logic = _world(3, dense=True)
    n = len(rows_logic.world_runtime.monster_things)
    for _ in range(int(2 * MONSTER_SHOOT_INTERVAL / TICK)):
        plain_ai._update_dense(plain_logic.world_runtime.monster_things, TICK, plain_logic.player_runtime.player.pos)
        rows_ai._update_dense(rows_logic.world_runtime.monster_things, np.full(n, TICK),
                              rows_logic.player_runtime.player.pos)
    assert _snapshot(plain_ai, plain_logic) == _snapshot(rows_ai, rows_logic)


def test_a_sit_out_row_skips_only_that_monster():
    ai, logic = _world(4, dense=True)
    monsters = logic.world_runtime.monster_things
    before = [tuple(m.pos) for m in monsters]
    sit = np.zeros(len(monsters), dtype=bool)
    sit[0] = True
    ai._update_dense(monsters, TICK, logic.player_runtime.player.pos, sit)
    assert tuple(monsters[0].pos) == before[0]
    assert any(tuple(m.pos) != b for m, b in zip(monsters[1:], before[1:]))


def test_a_zero_per_row_delta_is_still_a_tick_not_a_skip():
    """A zero-length row runs (it can still wake, sight and shoot); only the
    sit-out mask skips a row."""
    zero_ai, zero_logic = _world(6, dense=True)
    plain_ai, plain_logic = _world(6, dense=True)
    n = len(zero_logic.world_runtime.monster_things)
    zero_ai._update_dense(zero_logic.world_runtime.monster_things, np.zeros(n), zero_logic.player_runtime.player.pos)
    plain_ai._update_dense(plain_logic.world_runtime.monster_things, 0.0, plain_logic.player_runtime.player.pos)
    assert _snapshot(zero_ai, zero_logic) == _snapshot(plain_ai, plain_logic)


def _fit(logic, rect=(300.0, 300.0)):
    logic.plugins = SimpleNamespace(
        services={"bigworld": SimpleNamespace(
            tiers=SimpleNamespace(near_rect=rect)
        )}
    )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_a_fitted_view_runs_the_same_in_both_passes(seed):
    """Off-screen throttling goes through the dense pass and the per-monster
    reference alike, with the same per-row time."""
    dense_ai, dense_logic = _world(seed, dense=True)
    ref_ai, ref_logic = _world(seed, dense=False)
    _fit(dense_logic)
    _fit(ref_logic)
    for tick in range(int(4 * MONSTER_SHOOT_INTERVAL / TICK)):
        dense_ai.update(TICK)
        ref_ai.update(TICK)
        assert _snapshot(dense_ai, dense_logic) == _snapshot(ref_ai, ref_logic), tick


def test_off_screen_monsters_step_once_per_interval_and_on_screen_ones_every_tick():
    ai, logic = _world(2, dense=True)
    player = logic.player_runtime.player.pos
    _fit(logic, rect=(1.0e6, 1.0e6))             # everything on screen
    on = list(logic.world_runtime.monster_things)
    _fit(logic, rect=(0.0, 0.0))                 # everything off screen
    moved = []
    for _ in range(6):
        before = [tuple(m.pos) for m in on]
        ai.update(TICK)
        moved.append(any(tuple(m.pos) != b for m, b in zip(on, before)))
    # Five ticks sat out, the sixth carries all 0.2 s.
    assert moved[:5] == [False] * 5 and moved[5] is True
    assert player is logic.player_runtime.player.pos


def test_a_fitted_pass_keeps_nothing_past_stop():
    """The resident list a camera-fitted Big World pass keeps is released with
    the rest of the session's monsters, and an unfitted pass keeps none."""
    from engine.spatial import SIM_TIER_KEY, TIER_DORMANT

    ai, logic = _world(5, dense=True)
    ai.update(TICK)
    assert ai._tick_monsters is None                 # no Big World: nothing kept
    _fit(logic)
    parked = logic.world_runtime.monster_things[0]
    parked.properties[SIM_TIER_KEY] = TIER_DORMANT
    ai.update(TICK)
    assert ai._tick_monsters is not None and parked not in ai._tick_monsters
    ai.forget_monsters()
    assert ai._tick_monsters is None and ai._offscreen_accum == 0.0
    assert ai._owed == {}
