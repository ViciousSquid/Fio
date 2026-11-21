"""A stuck patrol's detour search, and a blast's shove, against walls.

A monster pressed into a wall searches for a detour PathNode. That search
used to walk every Thing in the world on every AI tick for as long as the
monster stayed stuck and found nothing (30 Hz, under the AI lock); the
PathNodes are now cached per thing list and a search that finds nothing waits
DETOUR_RETRY_TICKS before it is tried again from the same place.

A shove (``knock_back``) slides the monster through the same wall test as
walking; the slide is tested along its path, so a hard shove cannot carry
the monster's box clean over a thin wall between two ticks.
"""

import pytest

from editor.things import Thing
from tests.helpers.worlds import box_brush, make_thing

pytestmark = pytest.mark.qt

TICK = 1.0 / 30.0
FAR_PLAYER = (100000.0, 0.0, 0.0)      # out of sight, so patrol runs


def _stuck_world(monster_factory, path_node_factory, ai_world, flat_ground, clutter=0):
    # The monster stands inside a solid block: every step it tries overlaps a
    # wall, so it is stuck for as long as the test runs, and no detour node
    # exists to free it.
    block = box_brush("block", (0, 160, 0), (600, 320, 600))
    node = path_node_factory("goal", (2000, 96, 0))
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="goal")
    things = [monster, node] + [make_thing(Thing, "junk_%d" % i, (i * 4.0, 0, 9000))
                                for i in range(clutter)]
    ai, logic = ai_world(brushes=flat_ground + [block], things=things,
                         player_pos=FAR_PLAYER)
    logic.world_runtime.build_entity_caches()
    return ai, logic, monster


def test_a_stuck_monster_with_no_detour_does_not_search_every_tick(
        monster_factory, path_node_factory, ai_world, flat_ground):
    ai, _logic, monster = _stuck_world(monster_factory, path_node_factory,
                                       ai_world, flat_ground, clutter=2000)
    searches = []
    real = ai._find_nearby_detour_node

    def counted(*args, **kwargs):
        searches.append(args)
        return real(*args, **kwargs)

    ai._find_nearby_detour_node = counted
    ticks = 300                                     # ten seconds at 30 Hz
    for _ in range(ticks):
        ai.update(TICK)

    state = ai.monster_states[id(monster)]
    assert state.get("patrol_blocked_count", 0) > 100, "the monster never got stuck"
    assert not state.get("detour_node")
    # Was one search a tick from the stuck threshold on (~270); now one per
    # DETOUR_RETRY_TICKS.
    assert 1 <= len(searches) <= ticks // ai.DETOUR_RETRY_TICKS + 1, (
        "%d detour searches in %d ticks" % (len(searches), ticks))
    # And the searches walk the cached PathNodes, not the 2000 Things.
    assert ai.path_node_scans == 1


def test_moving_to_a_new_place_searches_again_at_once(
        monster_factory, path_node_factory, ai_world, flat_ground):
    ai, _logic, monster = _stuck_world(monster_factory, path_node_factory,
                                       ai_world, flat_ground)
    import glm
    state = {}
    here = glm.vec3(0.0, 96.0, 0.0)
    assert ai._detour_search_due(state, here, "goal", 40)
    state["_detour_miss"] = ("goal", ai._detour_cell(here), 40)
    assert not ai._detour_search_due(state, here, "goal", 41)
    assert ai._detour_search_due(state, here, "goal", 40 + ai.DETOUR_RETRY_TICKS)
    assert ai._detour_search_due(state, here + glm.vec3(100.0, 0.0, 0.0), "goal", 41)
    assert ai._detour_search_due(state, here, "other", 41)
    assert ai._detour_search_due(state, here, "goal", 5)       # a new spell


def test_the_path_node_cache_follows_the_thing_list(
        monster_factory, path_node_factory, ai_world, flat_ground):
    ai, logic, _monster = _stuck_world(monster_factory, path_node_factory,
                                       ai_world, flat_ground)
    assert [n.name for n in ai._path_nodes()] == ["goal"]
    logic.editor_state.things.append(path_node_factory("late", (100, 96, 0)))
    assert [n.name for n in ai._path_nodes()] == ["goal", "late"]
    assert ai.path_node_scans == 2


def test_a_detour_is_still_found(monster_factory, path_node_factory, ai_world,
                                 flat_ground):
    """The memo must not hide a detour that exists."""
    # A corner between the monster and its goal: both slides are blocked.
    walls = [box_brush("wall_x", (300, 160, 0), (64, 320, 2000)),
             box_brush("wall_z", (0, 160, 300), (2000, 320, 64))]
    goal = path_node_factory("goal", (1200, 96, 1200))
    side = path_node_factory("side", (-400, 96, -400))
    monster = monster_factory("walker", (150, 96, 150), patrol=True, patrol_target="goal")
    ai, logic = ai_world(brushes=flat_ground + walls, things=[monster, goal, side],
                         player_pos=FAR_PLAYER)
    logic.world_runtime.build_entity_caches()
    for _ in range(90):
        ai.update(TICK)
        if ai.monster_states[id(monster)].get("detour_node"):
            break
    assert ai.monster_states[id(monster)].get("detour_node") == "side"


def test_a_hard_shove_does_not_carry_a_monster_through_a_thin_wall(
        monster_factory, ai_world, flat_ground):
    # A 16-unit wall 150 units ahead (the monster's box reaches 100 either
    # side). A 30000 u/s shove would move it 1000 units in one tick: the end
    # point is well past the wall and clear of it.
    wall = box_brush("thin", (250, 160, 0), (16, 320, 4000))
    monster = monster_factory("pushed", (0, 96, 0), awake=False)
    ai, logic = ai_world(brushes=flat_ground + [wall], things=[monster],
                         player_pos=FAR_PLAYER)
    ai.knock_back(monster, 1.0, 0.0, 30000.0)
    for _ in range(10):
        ai._apply_knockback(TICK)
    assert float(monster.pos[0]) < 250.0 - 8.0, (
        "shoved through the wall to x=%.0f" % float(monster.pos[0]))
    assert float(monster.pos[0]) > 0.0                  # but it did slide up to it


def test_a_shove_without_walls_still_slides_its_full_distance(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("pushed", (0, 96, 0), awake=False)
    ai, _logic = ai_world(brushes=flat_ground, things=[monster],
                          player_pos=FAR_PLAYER)
    ai.knock_back(monster, 1.0, 0.0, 600.0)
    ai._apply_knockback(TICK)
    assert float(monster.pos[0]) == pytest.approx(600.0 * TICK, rel=1e-4)
