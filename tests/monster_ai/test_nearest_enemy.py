"""Nearest-enemy search: the dense batch must answer what the walk answered.

Every awake teamed monster without an aggro target used to walk every monster
to find its closest enemy -- the same arithmetic N times over rather than once
over N. The batch does all of them in one squared-distance matrix, three masks
and an ``argmin``.

The walk is still in the tree, as the batch's reference and as its fallback
below the size gate, so the two can be driven over the same monsters and
compared. These tests do that, with the cases most likely to separate them:
exact ties, teamless and dead and hidden monsters, the range boundary, and
monsters that are still falling -- which is when positions move most within a
tick and so when a batch is least like a per-monster search.
"""

import numpy as np
import pytest

from engine.monster_ai import MonsterAI
from engine.monster_constants import MONSTER_SIGHT_RANGE

pytestmark = pytest.mark.qt


def _walk(ai, monsters, max_range=MONSTER_SIGHT_RANGE):
    """What every monster's search answers on the per-monster path."""
    return [ai._find_closest_enemy_scalar(m, m.properties.get('team', ''),
                                          max_range)
            if m.properties.get('team', '') else None
            for m in monsters]


def _batched(ai, monsters, max_range=MONSTER_SIGHT_RANGE):
    """The same, answered from the tick's batch."""
    ai._enemy_ready = False
    ai._enemy_nearest = None
    return [ai._find_closest_enemy_team_monster(
        m, m.properties.get('team', ''), None, max_range) for m in monsters]


def _agree(ai, monsters, what, max_range=MONSTER_SIGHT_RANGE):
    want = _walk(ai, monsters, max_range)
    got = _batched(ai, monsters, max_range)
    if want == got:
        return want
    for i, (w, g) in enumerate(zip(want, got)):
        if w is not g:
            name = monsters[i].properties.get('name', '?')
            raise AssertionError(
                "%s: %s picked %r on the walk and %r in the batch"
                % (what, name,
                   w.properties.get('name') if w else None,
                   g.properties.get('name') if g else None))
    raise AssertionError("%s: the two disagree in length" % what)


def _teamed(monster_factory, specs):
    return [monster_factory("m%d" % i, pos, **props)
            for i, (pos, props) in enumerate(specs)]


def _world(ai_world, monsters):
    ai, logic = ai_world(things=monsters)
    return ai, logic


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_a_small_monster_set_keeps_the_walk(ai_world, monster_factory):
    """Below the gate the batch costs more to assemble than it saves."""
    monsters = _teamed(monster_factory, [
        ((float(i) * 100.0, 0.0, 0.0), {"team": "red" if i % 2 else "blue"})
        for i in range(4)])
    ai, _ = _world(ai_world, monsters)

    ai._enemy_ready = False
    assert ai._enemy_batch(MONSTER_SIGHT_RANGE) is None
    _agree(ai, monsters, "below the gate")


def test_the_batch_is_built_once_per_tick(ai_world, monster_factory):
    monsters = _teamed(monster_factory, [
        ((float(i) * 60.0, 0.0, 0.0), {"team": "red" if i % 2 else "blue"})
        for i in range(80)])
    ai, _ = _world(ai_world, monsters)

    built = []
    real = MonsterAI._nearest_enemy_rows
    MonsterAI._nearest_enemy_rows = staticmethod(
        lambda *a: built.append(1) or real(*a))
    try:
        ai._enemy_ready = False
        for m in monsters:
            ai._find_closest_enemy_team_monster(m, m.properties['team'], None,
                                                MONSTER_SIGHT_RANGE)
    finally:
        MonsterAI._nearest_enemy_rows = staticmethod(real)
    assert len(built) == 1, (
        "built %d times for %d queries in one tick" % (len(built), len(monsters)))


def test_a_map_with_no_teams_never_builds_a_batch(ai_world, monster_factory):
    monsters = _teamed(monster_factory,
                       [((float(i) * 60.0, 0.0, 0.0), {}) for i in range(80)])
    ai, _ = _world(ai_world, monsters)

    built = []
    real = MonsterAI._nearest_enemy_rows
    MonsterAI._nearest_enemy_rows = staticmethod(
        lambda *a: built.append(1) or real(*a))
    try:
        ai.update(1 / 30.0)
    finally:
        MonsterAI._nearest_enemy_rows = staticmethod(real)
    assert built == []


def test_a_range_the_batch_was_not_built_for_falls_back(ai_world, monster_factory):
    monsters = _teamed(monster_factory, [
        ((float(i) * 60.0, 0.0, 0.0), {"team": "red" if i % 2 else "blue"})
        for i in range(80)])
    ai, _ = _world(ai_world, monsters)

    ai._enemy_ready = False
    assert ai._enemy_batch(MONSTER_SIGHT_RANGE) is not None
    assert ai._enemy_batch(64.0) is None, (
        "a different range must not be answered from the batch")
    # ...and the answer is still right, via the walk.
    near = ai._find_closest_enemy_team_monster(
        monsters[0], monsters[0].properties['team'], None, 64.0)
    assert near is ai._find_closest_enemy_scalar(
        monsters[0], monsters[0].properties['team'], 64.0)


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

def test_the_batch_agrees_on_a_plain_two_team_field(ai_world, monster_factory):
    monsters = _teamed(monster_factory, [
        (((i % 10) * 130.0 - 600.0, 0.0, (i // 10) * 130.0 - 400.0),
         {"team": "red" if i % 2 else "blue"})
        for i in range(80)])
    ai, _ = _world(ai_world, monsters)
    found = _agree(ai, monsters, "two-team field")
    assert any(f is not None for f in found), "nobody found an enemy"


def test_an_exact_tie_resolves_to_the_same_monster(ai_world, monster_factory):
    """`argmin` takes the first minimum; the walk took a new best on `<`."""
    specs = [((0.0, 0.0, 0.0), {"team": "red"}),
             ((300.0, 0.0, 0.0), {"team": "blue"}),
             ((-300.0, 0.0, 0.0), {"team": "blue"})]
    specs += [((float(2000 + i * 50), 0.0, 0.0), {"team": "red"})
              for i in range(70)]
    monsters = _teamed(monster_factory, specs)
    ai, _ = _world(ai_world, monsters)

    found = _agree(ai, monsters, "exact tie")
    assert found[0] is monsters[1], (
        "the tie should resolve to the earlier monster in thing order")


def test_the_kernel_resolves_distance_at_the_walks_precision(ai_world,
                                                            monster_factory):
    """The batch must not be *more* precise than the walk it replaces.

    These three positions are lifted from a falling-monster tick that caught a
    float64 kernel. The querying monster is exactly 22509.0 from one enemy and
    22508.9999284744 from the other in float64 -- but both are exactly 22509.0
    in float32, which is what ``glm.dot`` computes. The walk therefore ties and
    keeps the earlier monster; a float64 batch separates them and picks the
    other one.

    Rounding a float64 result to float32 does not fix it either: that lands on
    22508.998 where the float32 computation lands on 22509.0. The precision has
    to be in the inputs and the intermediates.
    """
    specs = [((-303.0, 406.0, -400.0), {"team": "blue"}),          # the tie, first
             ((-153.0, 409.0, -400.0), {"team": "red"}),           # the querier
             ((-3.000000238418579, 412.0, -400.0), {"team": "blue"})]
    specs += [((0.0, float(9000 + i * 40), 0.0), {"team": "red"})
              for i in range(70)]
    monsters = _teamed(monster_factory, specs)
    ai, _ = _world(ai_world, monsters)

    found = _agree(ai, monsters, "float32 tie")
    assert found[1] is monsters[0], (
        "the tie should resolve to the earlier monster, as the walk's strict "
        "`<` does")


def test_teamless_dead_and_hidden_monsters_are_excluded(ai_world,
                                                        monster_factory):
    specs = [((0.0, 0.0, 0.0), {"team": "red"}),
             ((100.0, 0.0, 0.0), {"team": "blue", "dead": True}),
             ((150.0, 0.0, 0.0), {"team": "blue", "hidden": True}),
             ((200.0, 0.0, 0.0), {}),                       # teamless
             ((250.0, 0.0, 0.0), {"team": "red"}),          # same team
             ((400.0, 0.0, 0.0), {"team": "blue"})]
    specs += [((float(3000 + i * 40), 0.0, 0.0), {"team": "red"})
              for i in range(70)]
    monsters = _teamed(monster_factory, specs)
    ai, _ = _world(ai_world, monsters)

    found = _agree(ai, monsters, "excluded monsters")
    assert found[0] is monsters[5], (
        "the dead, hidden, teamless and same-team ones should all be skipped")


def test_the_range_boundary_agrees(ai_world, monster_factory):
    for delta in (-1.0, -0.001, 0.0, 0.001, 1.0):
        specs = [((0.0, 0.0, 0.0), {"team": "red"}),
                 ((MONSTER_SIGHT_RANGE + delta, 0.0, 0.0), {"team": "blue"})]
        specs += [((0.0, float(4000 + i * 40), 0.0), {"team": "red"})
                  for i in range(70)]
        monsters = _teamed(monster_factory, specs)
        ai, _ = _world(ai_world, monsters)
        _agree(ai, monsters, "range boundary at %+g" % delta)


def test_a_monster_with_no_enemy_in_range_finds_none(ai_world, monster_factory):
    specs = [((0.0, 0.0, 0.0), {"team": "red"}),
             ((MONSTER_SIGHT_RANGE * 3, 0.0, 0.0), {"team": "blue"})]
    specs += [((0.0, float(5000 + i * 40), 0.0), {"team": "red"})
              for i in range(70)]
    monsters = _teamed(monster_factory, specs)
    ai, _ = _world(ai_world, monsters)
    found = _agree(ai, monsters, "nothing in range")
    assert found[0] is None


def test_the_batch_agrees_over_a_random_sweep(ai_world, monster_factory):
    rng = np.random.default_rng(17)
    for trial in range(8):
        specs = []
        for i in range(80):
            pos = tuple(float(v) for v in rng.uniform(-1400, 1400, 3))
            props = {}
            team = rng.integers(0, 3)
            if team < 2:
                props["team"] = ("red", "blue")[int(team)]
            if rng.random() < 0.15:
                props["dead"] = True
            if rng.random() < 0.15:
                props["hidden"] = True
            specs.append((pos, props))
        monsters = _teamed(monster_factory, specs)
        ai, _ = _world(ai_world, monsters)
        _agree(ai, monsters, "random sweep %d" % trial)


def test_falling_monsters_agree_tick_after_tick(ai_world, monster_factory):
    """Positions move most within a tick while monsters are still falling,
    which is when a batch is least like a per-monster search."""
    from tests.helpers.worlds import room

    specs = [(((i % 9) * 150.0 - 600.0, 400.0 + i * 3.0,
               (i // 9) * 150.0 - 400.0),
              {"team": "red" if i % 2 else "blue"})
             for i in range(72)]
    monsters = _teamed(monster_factory, specs)
    ai, logic = ai_world(brushes=room(2048, 512, 2048), things=monsters)
    logic._monster_things = list(monsters)
    ai.set_spatial_grid(logic.build_spatial_grid())

    for tick in range(40):
        _agree(ai, monsters, "falling, tick %d" % tick)
        ai.update(1 / 30.0)


def test_the_batch_follows_a_monster_that_dies_mid_session(ai_world,
                                                           monster_factory):
    specs = [((0.0, 0.0, 0.0), {"team": "red"}),
             ((100.0, 0.0, 0.0), {"team": "blue"}),
             ((400.0, 0.0, 0.0), {"team": "blue"})]
    specs += [((float(6000 + i * 40), 0.0, 0.0), {"team": "red"})
              for i in range(70)]
    monsters = _teamed(monster_factory, specs)
    ai, _ = _world(ai_world, monsters)

    assert _agree(ai, monsters, "before the death")[0] is monsters[1]
    monsters[1].properties["dead"] = True
    assert _agree(ai, monsters, "after the death")[0] is monsters[2]
