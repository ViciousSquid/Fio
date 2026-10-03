"""The dense monster pass decides what the per-monster path decided.

``MonsterAI._update_dense`` runs a tick as columns over the monster table;
``MonsterAI._update_monster`` is the per-monster reference it replaced. Where
nothing depends on the order monsters act within a tick -- monsters hunting
the player, out of each other's line of fire -- the two must agree exactly,
tick after tick: positions to the bit, every flag the sprite and I/O read, the
state savegames persist, every hit on the player and every projectile.

Where order does matter (monsters fighting each other, whose targets move
during the tick) the dense pass is allowed to differ only as documented; the
fight test checks the invariants instead.
"""

import math
import random

import pytest

from editor.things import Monster
from engine.monster_ai import MonsterAI
from engine.monster_constants import MONSTER_SHOOT_INTERVAL
from tests.helpers.fakes import FakeLogicThread, FakePlayer
from tests.helpers.worlds import box_brush, make_thing

pytestmark = pytest.mark.qt

TICK = 1.0 / 30.0


def _world(seed, dense, teams=False, count=None):
    rng = random.Random(seed)
    brushes = [
        box_brush("ground", (0, -16, 0), (8192, 32, 8192)),
        # A raised platform some monsters start over, so they fall to it.
        box_brush("ledge", (900, 64, 0), (400, 32, 400)),
        # Walls monsters have to slide along on their way in.
        box_brush("wall_a", (0, 64, 700), (600, 160, 32)),
        box_brush("wall_b", (-700, 64, 0), (32, 160, 600)),
    ]
    things = []
    if teams:
        for i in range(count or 60):
            angle = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(200, 1400)
            things.append(make_thing(
                Monster, "m%02d" % i, (r * math.cos(angle), 96, r * math.sin(angle)),
                monster_type=rng.choice(["human", "flying"]),
                team="red" if i % 2 else "blue",
                awake=True, wake_on_sight=True, health=60, damage=15))
    else:
        # One team: nobody is anybody's enemy and crossfire never hits a
        # teammate, so every monster hunts the player and nothing depends on
        # which monster moved first this tick -- the one thing the two passes
        # may order apart. (Crossfire itself is checked separately below.)
        for i in range(4):
            angle = i * math.pi / 2 + 0.3
            r = rng.uniform(500, 900)
            things.append(make_thing(
                Monster, "human%d" % i, (r * math.cos(angle), 96, r * math.sin(angle)),
                monster_type="human", awake=True, health=100, damage=7, team="red",
                sprite_height=rng.choice([96, 128, 160])))
        for i in range(10):
            angle = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(300, 1300)
            things.append(make_thing(
                Monster, "flier%d" % i,
                (r * math.cos(angle), rng.uniform(60, 300), r * math.sin(angle)),
                monster_type="flying", awake=rng.random() < 0.7, team="red",
                wake_on_sight=True, health=100, damage=5))
        # Over the ledge: falls onto it.
        things.append(make_thing(Monster, "faller", (900, 400, 30),
                                 monster_type="human", awake=False,
                                 wake_on_sight=True, damage=3, team="red"))
        things.append(make_thing(Monster, "corpse", (-300, 350, -900),
                                 monster_type="human", dead=True, team="red"))
        things.append(make_thing(Monster, "ghost", (100, 96, -1500),
                                 monster_type="human", hidden=True, is_shooting=True, team="red"))
        things.append(make_thing(Monster, "statue", (-100, 96, -1500),
                                 monster_type="human", awake=True, disabled=True, team="red"))
        things.append(make_thing(Monster, "ambush", (0, 96, -2500),
                                 monster_type="human", triggered=True, team="red"))
        things.append(make_thing(Monster, "sleeper", (2500, 96, 2500),
                                 monster_type="human", awake=False, team="red"))
    logic = FakeLogicThread(brushes=brushes, things=things,
                            player=FakePlayer((0.0, 0.0, 0.0)))
    logic.player_health = 10 ** 9
    logic._monster_things = [t for t in logic.things if isinstance(t, Monster)]
    ai = MonsterAI(logic)
    ai.DENSE_UPDATE = dense
    ai.set_spatial_grid(logic.build_spatial_grid())
    return ai, logic


def _snapshot(ai, logic):
    monsters = []
    for m in logic._monster_things:
        p = m.properties
        state = ai.monster_states.get(id(m))
        monsters.append((
            tuple(m.pos),
            p.get('dead'), p.get('awake'), p.get('is_shooting'), p.get('_vel_y'),
            p.get('health'),
            None if state is None else (
                state['shoot_timer'], state['anim_timer'], state['in_sight'],
                state.get('vel_y')),
        ))
    store = logic._monster_projectiles
    projectiles = [
        (tuple(store.pos[i]), tuple(store.vel[i]), float(store.damage[i]))
        for i in range(len(store))
    ]
    return (monsters, list(logic.damage_applied), projectiles)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_dense_pass_matches_the_per_monster_path(seed):
    dense_ai, dense_logic = _world(seed, dense=True)
    ref_ai, ref_logic = _world(seed, dense=False)
    for tick in range(int(6 * MONSTER_SHOOT_INTERVAL / TICK)):
        dense_ai.update(TICK)
        ref_ai.update(TICK)
        dense = _snapshot(dense_ai, dense_logic)
        ref = _snapshot(ref_ai, ref_logic)
        if dense != ref:
            for i, (a, b) in enumerate(zip(dense[0], ref[0])):
                assert a == b, (tick, dense_logic._monster_things[i].name, a, b)
            assert dense[1] == ref[1], (tick, "player damage")
            assert dense[2] == ref[2], (tick, "projectiles")
    # The scenario is not vacuous: monsters moved, shot and fell.
    assert dense_logic.damage_applied or dense_logic._monster_projectiles
    faller = next(m for m in dense_logic._monster_things if m.name == "faller")
    assert faller.pos[1] < 400


def test_dense_pass_casts_rays_only_for_due_shots():
    ai, logic = _world(3, dense=True)
    rays = []
    real = ai._grid.has_line_of_sight

    def counting(*args):
        rays.append(args)
        return real(*args)

    ai._grid.has_line_of_sight = counting
    ticks = int(3 * MONSTER_SHOOT_INTERVAL / TICK)
    for _ in range(ticks):
        ai.update(TICK)
    awake = sum(1 for m in logic._monster_things if m.properties.get('awake'))
    # The per-monster path cast one ray per awake monster per tick.
    assert 0 < len(rays) < awake * ticks / 10


def test_a_two_team_fight_keeps_its_invariants():
    ai, logic = _world(11, dense=True, teams=True)
    ref_ai, ref_logic = _world(11, dense=False, teams=True)
    for _ in range(int(8 * MONSTER_SHOOT_INTERVAL / TICK)):
        before = {id(m): (m.properties.get('dead'), tuple(m.pos))
                  for m in logic._monster_things}
        ai.update(TICK)
        ref_ai.update(TICK)
        for m in logic._monster_things:
            was_dead, was_pos = before[id(m)]
            if was_dead:
                # A dead monster settles onto the ground under it (up or down,
                # to ground + half its height) and does nothing else.
                assert (m.pos[0], m.pos[2]) == (was_pos[0], was_pos[2])
                assert not m.properties.get('is_shooting')
    dead = sum(1 for m in logic._monster_things if m.properties.get('dead'))
    ref_dead = sum(1 for m in ref_logic._monster_things if m.properties.get('dead'))
    # Both fights happened, and at a comparable rate.
    assert dead > 0 and ref_dead > 0
    assert abs(dead - ref_dead) <= max(4, ref_dead // 3), (dead, ref_dead)


def test_table_crossfire_finds_the_monster_the_walk_finds():
    """Nearest sphere entry, same exclusions, earliest row on a tie."""
    import glm
    rng = random.Random(5)
    for trial in range(200):
        ai, logic = _world(trial, dense=True, teams=True, count=30)
        monsters = logic._monster_things
        for m in monsters:
            if rng.random() < 0.15:
                m.properties['dead'] = True
            if rng.random() < 0.05:
                m.properties['hidden'] = True
        ai.table.gather(monsters)
        shooter = rng.choice(monsters)
        start = glm.vec3(shooter.pos[0], shooter.pos[1] + 64.0, shooter.pos[2])
        end = glm.vec3(rng.uniform(-1500, 1500), rng.uniform(0, 200),
                       rng.uniform(-1500, 1500))
        expected = ai._find_monster_in_crossfire(shooter, start, end)
        got = ai._table_crossfire(shooter, start, end)
        assert got is expected, (trial, got and got.name, expected and expected.name)


def test_crossfire_skips_a_monster_killed_earlier_in_the_tick():
    """Who is alive is read once per tick; a kill since is still seen."""
    import glm
    rng = random.Random(9)
    checked = 0
    for trial in range(200):
        ai, logic = _world(trial, dense=True, teams=True, count=30)
        monsters = logic._monster_things
        ai.table.gather(monsters)
        shooter = rng.choice(monsters)
        start = glm.vec3(shooter.pos[0], shooter.pos[1] + 64.0, shooter.pos[2])
        end = glm.vec3(rng.uniform(-1500, 1500), rng.uniform(0, 200),
                       rng.uniform(-1500, 1500))
        first = ai._table_crossfire(shooter, start, end)
        if first is None:
            continue
        first.properties['dead'] = True              # the first shot killed it
        expected = ai._find_monster_in_crossfire(shooter, start, end)
        got = ai._table_crossfire(shooter, start, end)
        assert got is expected, (trial, got and got.name, expected and expected.name)
        checked += 1
    assert checked > 20
