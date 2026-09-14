"""Patrol: walking a chain of PathNodes when the target is out of sight.

A patrol route is a linked list of ``PathNode`` entities joined by
``next_node``.  MonsterAI builds the chain once, walks it, fires the node's
outputs as it arrives, waits and leaves, and advances according to the
monster's ``patrol_mode``.  The failure modes worth guarding are the ones that
are silent in play: a chain that loops forever because a cycle was not detected,
a node that rejects the monster's type, a target that names nothing, and an
index that walks off the end of the chain.
"""

import pytest

from engine.monster_constants import MONSTER_MOVE_SPEED

pytestmark = pytest.mark.qt

TICK = 1.0 / 30.0
FAR_PLAYER = (100000.0, 0.0, 0.0)      # out of sight, so patrol runs


def _state(ai, monster):
    return ai.monster_states[id(monster)]


def _chain_of(nodes):
    """Link a list of PathNodes head-to-tail via ``next_node``."""
    for node, nxt in zip(nodes, nodes[1:]):
        node.properties["next_node"] = nxt.name
    return nodes


# ---------------------------------------------------------------------------
# Chain building
# ---------------------------------------------------------------------------

def test_a_chain_is_built_from_the_next_node_links(monster_factory,
                                                   path_node_factory, ai_world,
                                                   flat_ground):
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 300, 96, 0))
                       for i in range(3)])
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    ai.update(TICK)

    assert _state(ai, monster)["patrol_chain"] == ["n0", "n1", "n2"], (
        "chain built as %s, expected n0 -> n1 -> n2"
        % (_state(ai, monster)["patrol_chain"],))


def test_a_cyclic_chain_terminates(monster_factory, path_node_factory, ai_world,
                                   flat_ground):
    """A ring of nodes must not make the chain builder loop for ever."""
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 300, 96, 0))
                       for i in range(4)])
    nodes[-1].properties["next_node"] = "n0"        # close the ring
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    ai.update(TICK)

    assert _state(ai, monster)["patrol_chain"] == ["n0", "n1", "n2", "n3"], (
        "a 4-node ring produced the chain %s; each node must appear once"
        % (_state(ai, monster)["patrol_chain"],))


def test_a_patrol_target_that_names_nothing_is_reported_once_and_does_not_move(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("walker", (0, 96, 0), patrol=True,
                              patrol_target="nowhere")
    ai, logic = ai_world(brushes=flat_ground, things=[monster],
                         player_pos=FAR_PLAYER)
    before = list(monster.pos)

    for _ in range(20):
        ai.update(TICK)

    assert monster.pos[0] == pytest.approx(before[0]), \
        "'%s' moved toward a patrol target that does not exist" % monster.name
    assert _state(ai, monster)["patrol_warn_missing"] == "nowhere", (
        "the missing target should be recorded so the warning is not repeated "
        "every tick; got %r" % _state(ai, monster).get("patrol_warn_missing"))


def test_a_node_that_rejects_the_monster_type_ends_the_chain(
        monster_factory, path_node_factory, ai_world, flat_ground):
    a = path_node_factory("a", (300, 96, 0), affects_type="human")
    b = path_node_factory("b", (600, 96, 0), affects_type="flying")
    a.properties["next_node"] = "b"
    monster = monster_factory("walker", (0, 96, 0), monster_type="human",
                              patrol=True, patrol_target="a")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, a, b],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    ai.update(TICK)

    assert _state(ai, monster)["patrol_chain"] == ["a"], (
        "node 'b' only accepts flying monsters, so a human's chain stops at "
        "'a'; got %s" % (_state(ai, monster)["patrol_chain"],))


def test_patrol_is_ignored_when_the_flag_is_off(monster_factory, path_node_factory,
                                                ai_world, flat_ground):
    node = path_node_factory("n0", (500, 96, 0))
    monster = monster_factory("idle", (0, 96, 0), patrol=False, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()
    before = list(monster.pos)

    for _ in range(20):
        ai.update(TICK)

    assert monster.pos[0] == pytest.approx(before[0]), \
        "a monster with patrol=False walked toward its patrol_target"


# ---------------------------------------------------------------------------
# Walking the route
# ---------------------------------------------------------------------------

def test_a_patrolling_monster_walks_toward_its_current_node(
        monster_factory, path_node_factory, ai_world, flat_ground):
    node = path_node_factory("n0", (900, 96, 0), radius=32.0)
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    ai.update(TICK)      # builds the chain
    x_after_build = monster.pos[0]
    ai.update(TICK)

    travelled = monster.pos[0] - x_after_build
    assert travelled == pytest.approx(MONSTER_MOVE_SPEED * TICK, rel=1e-3), (
        "'%s' moved %.4f units in one tick toward its node; at %.1f units/s "
        "over %.4fs it should move %.4f"
        % (monster.name, travelled, MONSTER_MOVE_SPEED, TICK,
           MONSTER_MOVE_SPEED * TICK))


def test_arriving_at_a_node_fires_onmonsterarrived_once(
        monster_factory, path_node_factory, ai_world, flat_ground, io_manager):
    node = path_node_factory("n0", (40, 96, 0), radius=64.0)
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.io_manager = io_manager
    logic.rebuild_name_cache()

    for _ in range(3):
        ai.update(TICK)

    arrivals = [c for c in io_manager.fired if c[1] == "OnMonsterArrived"]
    assert len(arrivals) == 1, (
        "OnMonsterArrived fired %d times for a single arrival; outputs were %s"
        % (len(arrivals), io_manager.names()))
    assert arrivals[0][0] is node


def test_a_wait_time_holds_the_monster_and_fires_the_wait_outputs(
        monster_factory, path_node_factory, ai_world, flat_ground, io_manager):
    node = path_node_factory("n0", (0, 96, 0), radius=64.0, wait_time=1.0)
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.io_manager = io_manager
    logic.rebuild_name_cache()

    ai.update(TICK)
    assert _state(ai, monster)["patrol_waiting"] is True, (
        "the monster is on a node with wait_time=1.0 but is not waiting")
    assert "OnWaitStart" in io_manager.names()

    # Half a second in: still waiting.
    for _ in range(15):
        ai.update(TICK)
    assert _state(ai, monster)["patrol_waiting"] is True, (
        "the wait ended after ~0.5s; wait_time is 1.0s "
        "(remaining %.3f)" % _state(ai, monster)["patrol_wait_remaining"])

    # Past a second: the wait ends.
    for _ in range(20):
        ai.update(TICK)
    assert "OnWaitEnd" in io_manager.names(), (
        "the 1.0s wait never ended; outputs were %s" % io_manager.names())


# ---------------------------------------------------------------------------
# Patrol modes
# ---------------------------------------------------------------------------

def _walk_until_index(ai, monster, target_index, max_ticks=4000):
    for _ in range(max_ticks):
        ai.update(TICK)
        if _state(ai, monster).get("patrol_chain_idx") == target_index:
            return True
    return False


def test_loop_mode_returns_to_the_first_node(monster_factory, path_node_factory,
                                             ai_world, flat_ground):
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 200, 96, 0), radius=48.0)
                       for i in range(3)])
    monster = monster_factory("walker", (0, 96, 0), patrol=True,
                              patrol_target="n0", patrol_mode="loop")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    assert _walk_until_index(ai, monster, 2), "never reached the last node"
    assert _walk_until_index(ai, monster, 0), (
        "loop mode did not wrap back to the first node; index stuck at %s"
        % _state(ai, monster).get("patrol_chain_idx"))
    assert _state(ai, monster).get("patrol_finished") is not True


def test_once_mode_stops_at_the_last_node(monster_factory, path_node_factory,
                                          ai_world, flat_ground):
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 200, 96, 0), radius=48.0)
                       for i in range(3)])
    monster = monster_factory("walker", (0, 96, 0), patrol=True,
                              patrol_target="n0", patrol_mode="once")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    for _ in range(4000):
        ai.update(TICK)
        if _state(ai, monster).get("patrol_finished"):
            break

    assert _state(ai, monster).get("patrol_finished") is True, (
        "'once' mode never finished; index=%s x=%.1f"
        % (_state(ai, monster).get("patrol_chain_idx"), monster.pos[0]))
    resting_x = monster.pos[0]
    for _ in range(60):
        ai.update(TICK)
    assert monster.pos[0] == pytest.approx(resting_x), \
        "a finished patrol kept moving"


def test_ping_pong_mode_reverses_at_the_end(monster_factory, path_node_factory,
                                            ai_world, flat_ground):
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 200, 96, 0), radius=48.0)
                       for i in range(3)])
    monster = monster_factory("walker", (0, 96, 0), patrol=True,
                              patrol_target="n0", patrol_mode="ping_pong")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    assert _walk_until_index(ai, monster, 2), "never reached the last node"
    assert _walk_until_index(ai, monster, 1), (
        "ping_pong did not turn around at the end; index is %s"
        % _state(ai, monster).get("patrol_chain_idx"))
    assert _state(ai, monster)["patrol_chain_dir"] == -1, (
        "direction should be -1 after reversing, got %s"
        % _state(ai, monster)["patrol_chain_dir"])


def test_an_unknown_patrol_mode_falls_back_to_loop(monster_factory,
                                                   path_node_factory, ai_world,
                                                   flat_ground):
    nodes = _chain_of([path_node_factory("n%d" % i, (i * 200, 96, 0), radius=48.0)
                       for i in range(2)])
    monster = monster_factory("walker", (0, 96, 0), patrol=True,
                              patrol_target="n0", patrol_mode="sideways")
    ai, logic = ai_world(brushes=flat_ground, things=[monster] + nodes,
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    assert _walk_until_index(ai, monster, 1)
    assert _walk_until_index(ai, monster, 0), (
        "an unrecognised patrol_mode should behave as 'loop'")


# ---------------------------------------------------------------------------
# Patrol against a changing world
# ---------------------------------------------------------------------------

def test_changing_the_patrol_target_rebuilds_the_chain(monster_factory,
                                                       path_node_factory, ai_world,
                                                       flat_ground):
    a = path_node_factory("a", (300, 96, 0))
    b = path_node_factory("b", (-300, 96, 0))
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="a")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, a, b],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()

    ai.update(TICK)
    assert _state(ai, monster)["patrol_chain"] == ["a"]

    monster.properties["patrol_target"] = "b"
    ai.update(TICK)
    assert _state(ai, monster)["patrol_chain"] == ["b"], (
        "the chain was not rebuilt after patrol_target changed; it is still %s"
        % (_state(ai, monster)["patrol_chain"],))


def test_deleting_the_current_node_mid_patrol_clears_the_chain(
        monster_factory, path_node_factory, ai_world, flat_ground):
    node = path_node_factory("n0", (900, 96, 0))
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()
    ai.update(TICK)
    assert _state(ai, monster)["patrol_chain"] == ["n0"]

    logic.things.remove(node)
    logic.rebuild_name_cache()
    ai.update(TICK)

    assert _state(ai, monster)["patrol_chain"] == [], (
        "the chain still names a deleted node: %s"
        % (_state(ai, monster)["patrol_chain"],))


def test_seeing_the_player_overrides_patrol(monster_factory, path_node_factory,
                                            ai_world, flat_ground):
    node = path_node_factory("n0", (900, 96, 0))
    monster = monster_factory("walker", (300, 96, 0), patrol=True,
                              patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=(0.0, 96.0, 0.0))
    logic.rebuild_name_cache()

    for _ in range(10):
        ai.update(TICK)

    assert monster.pos[0] < 300.0, (
        "'%s' walked to its patrol node at x=900 while the player was visible "
        "at x=0; it is at x=%.1f" % (monster.name, monster.pos[0]))


def test_turning_patrol_off_clears_the_chain_state(monster_factory,
                                                   path_node_factory, ai_world,
                                                   flat_ground):
    node = path_node_factory("n0", (0, 96, 0), radius=64.0)
    monster = monster_factory("walker", (0, 96, 0), patrol=True, patrol_target="n0")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, node],
                         player_pos=FAR_PLAYER)
    logic.rebuild_name_cache()
    ai.update(TICK)
    assert _state(ai, monster)["patrol_at_target"] is True

    monster.properties["patrol"] = False
    ai.update(TICK)

    state = _state(ai, monster)
    assert state["patrol_at_target"] is False and state["patrol_chain"] == [], (
        "turning patrol off left stale route state: at_target=%r chain=%s"
        % (state["patrol_at_target"], state["patrol_chain"]))
