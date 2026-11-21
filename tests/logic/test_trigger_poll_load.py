"""The trigger poll's load: how much one poll does, and when polls fall due.

The broad phase used to build a dense (player + props + monsters) x (due
triggers) boolean matrix -- with every prop and monster in it even when every
due trigger was player-only -- and every trigger at the default 1 s interval
fell due on the same scheduler tick: 10k entities x 1k triggers was a 122 ms
spike once a second. Now only the categories a due trigger filters on are
gathered, the overlap test is a sorted-axis sweep per category, and triggers
with the same interval are staggered round-robin over the scheduler ticks.
Which triggers fire for whom is unchanged; so is each trigger's rate.
"""

import numpy as np
import pytest

from engine.logic_triggers import LogicTriggers
from tests.logic.test_trigger_filters import _logic, _thing

pytestmark = pytest.mark.qt


def _dense_reference(positions, type_mask, bounds, filter_masks):
    """The dense test the sweep replaced, written out as the specification."""
    inside = (
        (positions[:, None, 0] >= bounds[None, :, 0]) &
        (positions[:, None, 0] <= bounds[None, :, 3]) &
        (positions[:, None, 1] >= bounds[None, :, 1]) &
        (positions[:, None, 1] <= bounds[None, :, 4]) &
        (positions[:, None, 2] >= bounds[None, :, 2]) &
        (positions[:, None, 2] <= bounds[None, :, 5])
    )
    inside &= (type_mask[:, None] & filter_masks[None, :]) != 0
    return set(zip(*(a.tolist() for a in np.nonzero(inside))))


def _random_case(rng, entities, triggers, span=1.0e4, box=100.0):
    positions = (rng.random((entities, 3)) * span).astype(np.float32)
    type_mask = rng.choice(np.array([1, 2, 4], dtype=np.uint8), entities)
    type_mask[0] = 1
    lo = rng.random((triggers, 3)) * span
    bounds = np.concatenate([lo, lo + rng.random((triggers, 3)) * box + 1.0],
                            axis=1).astype(np.float32)
    filter_masks = rng.integers(1, 8, triggers).astype(np.uint8)
    return positions, type_mask, bounds, filter_masks


def test_the_sweep_finds_exactly_what_the_dense_test_found():
    rng = np.random.default_rng(3)
    runtime = LogicTriggers.__new__(LogicTriggers)
    runtime.pairs_tested = 0
    for entities, triggers, span in ((1, 1, 10.0), (50, 7, 200.0), (800, 120, 2000.0),
                                     (3000, 40, 500.0)):
        case = _random_case(rng, entities, triggers, span=span, box=span * 0.2)
        # Boundaries are inclusive: put entities exactly on some box faces.
        positions, _types, bounds, _filters = case
        positions[1:min(entities, 6), 0] = bounds[0, 0]
        positions[1:min(entities, 6), 1:] = bounds[0, 4:]
        got = runtime._trigger_overlaps(*case)
        assert set(zip(got[0].tolist(), got[1].tolist())) == _dense_reference(*case)


def test_nan_positions_and_bounds_are_misses_as_before():
    runtime = LogicTriggers.__new__(LogicTriggers)
    runtime.pairs_tested = 0
    positions = np.array([[5, 5, 5], [np.nan, 5, 5], [5, np.nan, 5]], dtype=np.float32)
    types = np.array([1, 2, 2], dtype=np.uint8)
    bounds = np.array([[0, 0, 0, 10, 10, 10], [np.nan, 0, 0, 10, 10, 10]], dtype=np.float32)
    filters = np.array([7, 7], dtype=np.uint8)
    got = runtime._trigger_overlaps(positions, types, bounds, filters)
    assert set(zip(got[0].tolist(), got[1].tolist())) == {(0, 0)}
    assert _dense_reference(positions, types, bounds, filters) == {(0, 0)}


def test_perf_the_sweep_tests_candidates_not_the_product():
    """10k entities x 1k small triggers: the work counted is a sliver of the
    10M-pair matrix the dense test built."""
    rng = np.random.default_rng(11)
    runtime = LogicTriggers.__new__(LogicTriggers)
    runtime.pairs_tested = 0
    case = _random_case(rng, 10000, 1000)
    runtime._trigger_overlaps(*case)
    assert runtime.pairs_tested < 10000 * 1000 // 50, runtime.pairs_tested


def test_a_player_only_poll_never_gathers_the_props_or_monsters():
    props = [_thing('prop', x=5 + i % 10, z=5) for i in range(300)]
    monsters = [_thing('monster', x=5, z=5 + i % 10) for i in range(300)]
    logic = _logic(props=props, monsters=monsters)        # default filter: player
    triggers = logic.trigger_runtime
    triggers._poll_triggers()
    assert logic._events == [('enter', 'player')]
    assert triggers.pairs_tested == 1                      # the player, once


def test_a_props_filter_still_sees_every_prop():
    props = [_thing('prop', x=5 + i % 5, z=5) for i in range(30)]
    logic = _logic(props=props, monsters=[_thing('monster')], filters=['props'])
    logic.trigger_runtime._poll_triggers()
    assert logic._events == [('enter', 'props')] * 30


def _many_triggers(logic, count, interval=None):
    brushes = []
    for i in range(count):
        brush = {'id': i + 1, 'pos': [i * 100, 0, 0], 'size': [20, 20, 20],
                 'is_trigger': True, 'trigger_type': 'Multiple',
                 'trigger_activation': 'touch', 'trigger_action': 'target'}
        if interval is not None:
            brush['trigger_poll_interval'] = interval
        brushes.append(brush)
    logic.editor_state.brushes = brushes
    logic.trigger_runtime.rebuild_trigger_index(brushes)
    logic.trigger_runtime._reset_trigger_state()


def _poll_log(logic, ticks):
    log = []
    original = logic.trigger_runtime._poll_triggers

    def tracked(use_key_pressed=False, trigger_ids=None):
        log.append(set(trigger_ids))
        original(use_key_pressed=use_key_pressed, trigger_ids=trigger_ids)

    logic.trigger_runtime._poll_triggers = tracked
    for _ in range(ticks):
        logic.trigger_runtime._handle_triggers(False, 1.0 / 60.0)
    return log


@pytest.mark.parametrize('interval, per_tick', [(1.0, 2), (0.5, 4), (0.25, 8)])
def test_triggers_with_one_interval_share_the_scheduler_ticks(interval, per_tick):
    logic = _logic()
    _many_triggers(logic, 8, interval)
    log = _poll_log(logic, 120)                            # two seconds
    # No scheduler tick polls them all at once (unless every tick must) ...
    assert max(len(due) for due in log) == per_tick
    # ... and each is still polled exactly once per interval.
    for bid in range(1, 9):
        assert sum(bid in due for due in log) == round(2.0 / interval)


def test_staggering_is_deterministic_and_never_makes_a_first_poll_later():
    logic = _logic()
    _many_triggers(logic, 8)
    first = {}
    for tick, due in enumerate(_poll_log(logic, 60)):
        for bid in due:
            first.setdefault(bid, tick)
    assert len(first) == 8
    assert max(first.values()) <= 60                       # within one interval
    again = _logic()
    _many_triggers(again, 8)
    first_again = {}
    for tick, due in enumerate(_poll_log(again, 60)):
        for bid in due:
            first_again.setdefault(bid, tick)
    assert first == first_again
