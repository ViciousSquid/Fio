"""Monster projectiles against monsters, tested as one batch per tick.

The per-projectile walk over every Thing was 140 ms of a 147 ms logic tick
with 500 monsters fighting (and ran inside the monster lock). The batched test
must hit exactly the monster the walk hit: the first one in ``things`` order
within the hit sphere that is not the owner, not on the owner's team, and not
dead or hidden.
"""

import math
import random

import numpy as np
import pytest

from engine.logic_combat import ProjectileStore
from tests.helpers.worlds import make_thing

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")
from editor.things import Monster, LogicRelay  # noqa: E402

pytestmark = pytest.mark.qt


def _logic(things, projectiles):
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = []
    state.things = list(things)
    logic = LogicThread(ThreadedGameState(), state)
    logic.world_runtime.build_entity_caches()
    for position, owner in projectiles:
        logic.combat_runtime._monster_projectiles.add(
            position, (0.0, 0.0, 0.0), id(owner), 5, 5.0)
    return logic


def _reference_hit(things, pos, owner_id):
    """The walk the batch replaced, written out as the specification."""
    owner = next((t for t in things if id(t) == owner_id), None)
    owner_team = owner.properties.get('team', '') if owner is not None else None
    for thing in things:
        if not isinstance(thing, Monster) or id(thing) == owner_id:
            continue
        if thing.properties.get('dead') or thing.properties.get('hidden'):
            continue
        team = thing.properties.get('team', '')
        if owner_team and team and owner_team == team:
            continue
        centre = (thing.pos[0], thing.pos[1] + 64.0, thing.pos[2])
        if math.dist(pos, centre) < 64.0:
            return thing.properties['name']
    return None





def test_published_projectile_snapshot_does_not_alias_simulation_store():
    owner = make_thing(Monster, "owner", (0, 0, 0), team="red")
    logic = _logic([owner], [((10, 20, 30), owner)])

    snapshot = logic.combat_runtime._publish_projectile_render_snapshot()
    assert snapshot.shape == (1, 3)
    assert snapshot.dtype == np.float32
    assert not np.shares_memory(snapshot, host.combat_runtime._monster_projectiles.pos)

    expected = snapshot.copy()
    logic.combat_runtime._monster_projectiles.pos[0] = (100.0, 200.0, 300.0)

    np.testing.assert_array_equal(snapshot, expected)


def test_projectile_store_is_numeric_and_dense():
    store = ProjectileStore()
    store.add((1.0, 2.0, 3.0), (4.0, 5.0, 6.0), 7, 8, 9)
    assert len(store) == 1
    assert store.pos.dtype == np.float32
    assert store.vel.dtype == np.float32
    assert store.owner_id.dtype == np.int64
    assert store.damage[0] == 8
    assert store.lifetime[0] == 9


def test_small_projectile_set_uses_scalar_path_but_matches_dense():
    owner = make_thing(Monster, "owner", (0, 0, 0), team="red")
    target = make_thing(Monster, "target", (10, 0, 0), team="blue")
    things = [owner, target]
    projectile = ((0, 64, 0), owner)

    dense = _logic(things, [projectile])
    scalar = _logic(things, [projectile])
    scalar.combat_runtime.PROJECTILE_DENSE_THRESHOLD = 0

    dense_before = {m.properties["name"]: m.properties.get("health", 100) for m in things}
    dense.combat_runtime._update_monster_projectiles(0.0)
    scalar.combat_runtime._update_monster_projectiles(0.0)

    dense_after = {m.properties["name"]: m.properties.get("health", 100) for m in dense.world_runtime.monster_things}
    scalar_after = {m.properties["name"]: m.properties.get("health", 100) for m in scalar.world_runtime.monster_things}
    assert dense_after == scalar_after
    assert dense_after["target"] == dense_before["target"] - 5
    assert len(dense.combat_runtime._monster_projectiles) == len(scalar.combat_runtime._monster_projectiles) == 0


def test_the_first_eligible_monster_in_order_is_hit():
    owner = make_thing(Monster, "owner", (0, 0, 0), team="red")
    things = [
        make_thing(LogicRelay, "relay", (0, 64, 0)),
        owner,
        make_thing(Monster, "ally", (5, 0, 0), team="red"),
        make_thing(Monster, "corpse", (5, 0, 0), team="blue", dead=True),
        make_thing(Monster, "ghost", (5, 0, 0), team="blue", hidden=True),
        make_thing(Monster, "target", (10, 0, 0), team="blue"),
        make_thing(Monster, "second", (0, 0, 10), team="blue"),
        make_thing(Monster, "far", (500, 0, 0), team="blue"),
    ]
    logic = _logic(things, [((0, 64, 0), owner)])
    before = {m.properties["name"]: m.properties.get("health", 100) for m in logic.world_runtime.monster_things}
    logic.combat_runtime._update_monster_projectiles(0.0)
    after = {m.properties["name"]: m.properties.get("health", 100) for m in logic.world_runtime.monster_things}
    changed = [name for name in after if after[name] != before[name]]
    assert changed == ["target"]
    assert after["target"] == before["target"] - 5
    assert len(logic.combat_runtime._monster_projectiles) == 0


def test_batch_matches_the_walk_over_random_crowds():
    rng = random.Random(20240914)
    for trial in range(40):
        things = []
        for i in range(60):
            things.append(make_thing(
                Monster, "m%02d_%d" % (trial, i),
                (rng.uniform(-150, 150), rng.uniform(-40, 40), rng.uniform(-150, 150)),
                team=rng.choice(["", "red", "blue"]),
                dead=rng.random() < 0.15, hidden=rng.random() < 0.1))
        owner = rng.choice(things)
        pos = (rng.uniform(-150, 150), rng.uniform(0, 100), rng.uniform(-150, 150))
        expected = _reference_hit(things, pos, id(owner))
        logic = _logic(things, [(pos, owner)])
        before = {m.properties["name"]: m.properties.get("health", 100) for m in logic.world_runtime.monster_things}
        logic.combat_runtime._update_monster_projectiles(0.0)
        changed = [name for name in before
                   if logic.world_runtime.find_entity_by_name(name).properties.get("health", 100) != before[name]]
        assert changed == ([expected] if expected else []), trial


def test_a_monster_killed_by_one_projectile_is_not_hit_by_the_next():
    owner = make_thing(Monster, "owner", (0, 0, 0), team="red")
    first = make_thing(Monster, "first", (0, 0, 20), team="blue")
    second = make_thing(Monster, "second", (0, 0, 40), team="blue")
    things = [owner, first, second]
    first.properties["health"] = 5
    logic = _logic(things, [((0, 64, 30), owner),
                            ((0, 64, 30), owner)])
    logic.combat_runtime._update_monster_projectiles(0.0)
    assert first.properties["dead"] is True
    assert second.properties["health"] == 95
    assert len(logic.combat_runtime._monster_projectiles) == 0
