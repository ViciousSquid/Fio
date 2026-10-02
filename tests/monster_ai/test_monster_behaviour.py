"""MonsterAI behaviour: waking, chasing, attacking, dying.

Every test here drives ``MonsterAI.update(delta)`` with an explicit delta and a
real :class:`engine.physics.SpatialGrid`, so nothing depends on wall-clock time
or on a fake that answers more kindly than the engine would.  The AI's state
machine lives in ``monster_states[id(thing)]`` and in the monster's own
properties; assertions name the monster and the state key rather than checking
a bare boolean.
"""

import math

import glm
import pytest

from engine.monster_constants import (
    MONSTER_MOVE_SPEED, MONSTER_SHOOT_ANIM_TIME, MONSTER_SHOOT_INTERVAL,
    MONSTER_SIGHT_RANGE, MONSTER_SPRITE_SIZES, MONSTER_STOP_DISTANCE,
)
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt

TICK = 1.0 / 30.0     # the MonsterAI thread's tick rate


def _state(ai, monster):
    return ai.monster_states.get(id(monster))


def _distance(a, b):
    return glm.length(glm.vec3(a) - glm.vec3(b))


# ---------------------------------------------------------------------------
# Creation and configuration
# ---------------------------------------------------------------------------

def test_a_new_monster_has_the_documented_defaults(monster_factory):
    monster = monster_factory("m")
    props = monster.properties
    assert props["type"] == "monster"
    assert props["monster_type"] == "human"
    assert props["patrol"] is False
    assert props["patrol_mode"] == "loop"
    assert props["triggered"] is False
    assert props["variant"] == "<None>"


@pytest.mark.parametrize("mtype", sorted(MONSTER_SPRITE_SIZES))
def test_sprite_size_defaults_come_from_the_monster_type(monster_factory, mtype):
    monster = monster_factory("m", monster_type=mtype)
    expected_w, expected_h = MONSTER_SPRITE_SIZES[mtype]
    assert (monster.properties["sprite_width"],
            monster.properties["sprite_height"]) == (expected_w, expected_h), (
        "a %s monster got sprite size (%s, %s), the type's default is (%d, %d)"
        % (mtype, monster.properties["sprite_width"],
           monster.properties["sprite_height"], expected_w, expected_h))


def test_per_monster_state_is_created_on_the_first_update(monster_factory, ai_world,
                                                          flat_ground):
    monster = monster_factory("m", (200, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    assert _state(ai, monster) is None, "state must not exist before the first tick"

    ai.update(TICK)

    state = _state(ai, monster)
    assert state is not None, "no state was created for '%s'" % monster.name
    assert state["shoot_timer"] == pytest.approx(MONSTER_SHOOT_INTERVAL - TICK), (
        "a monster in sight range starts its cooldown at %.2fs and spends one "
        "tick of it; shoot_timer is %.4f"
        % (MONSTER_SHOOT_INTERVAL, state["shoot_timer"]))


# ---------------------------------------------------------------------------
# Waking
# ---------------------------------------------------------------------------

def test_a_sleeping_monster_wakes_when_the_player_comes_into_sight(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("sleeper", (MONSTER_SIGHT_RANGE + 200, 96, 0),
                              awake=False)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ai.update(TICK)
    assert monster.properties["awake"] is False, (
        "'%s' is %.0f units away (sight range %d) and must stay asleep"
        % (monster.name, MONSTER_SIGHT_RANGE + 200, MONSTER_SIGHT_RANGE))

    monster.pos = [MONSTER_SIGHT_RANGE - 200, 96, 0]
    ai.update(TICK)
    assert monster.properties["awake"] is True, (
        "'%s' is now %.0f units away and should have woken"
        % (monster.name, MONSTER_SIGHT_RANGE - 200))


def test_a_monster_with_wake_on_sight_off_wakes_immediately(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("always_awake", (5000, 96, 0),
                              awake=False, wake_on_sight=False)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    ai.update(TICK)
    assert monster.properties["awake"] is True, (
        "wake_on_sight=False means the monster is active from the start")


def test_a_triggered_ambush_monster_ignores_sight(monster_factory, ai_world,
                                                  flat_ground):
    monster = monster_factory("ambush", (100, 96, 0), awake=False, triggered=True)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    ai.update(TICK)
    assert monster.properties["awake"] is False, (
        "a scripted ambush waits for its I/O trigger; it woke to sight from "
        "%.0f units" % _distance(monster.pos, logic.player.pos))
    assert _state(ai, monster) is None, \
        "a sleeping monster must not accumulate AI state"


def test_a_hearing_monster_wakes_to_a_noise_out_of_sight(monster_factory, ai_world,
                                                         flat_ground):
    far = MONSTER_SIGHT_RANGE + 500
    monster = monster_factory("ears", (far, 96, 0), awake=False, can_hear=True,
                              sight=MONSTER_SIGHT_RANGE)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ai.update(TICK)
    assert monster.properties["awake"] is False

    logic.emit_noise((far - 100, 96, 0), source="gunfire", loudness=1.0)
    ai.update(TICK)
    assert monster.properties["awake"] is True, (
        "'%s' can hear and a gunshot went off 100 units away" % monster.name)


def test_a_deaf_monster_ignores_noise(monster_factory, ai_world, flat_ground):
    far = MONSTER_SIGHT_RANGE + 500
    monster = monster_factory("deaf", (far, 96, 0), awake=False, can_hear=False)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    logic.emit_noise((far, 96, 0), source="gunfire", loudness=1.0)
    ai.update(TICK)
    assert monster.properties["awake"] is False


def test_a_quiet_noise_carries_less_far_than_a_loud_one(monster_factory, ai_world,
                                                        flat_ground):
    """Reach is the hearing range scaled by the event's loudness."""
    distance = 600.0
    quiet = monster_factory("quiet_listener", (distance, 96, 0), awake=False,
                            can_hear=True, sight=1000.0)
    ai, logic = ai_world(brushes=flat_ground, things=[quiet],
                         player_pos=(5000.0, 0.0, 0.0))
    logic.emit_noise((0, 96, 0), source="splash", loudness=0.25)   # reach 250
    ai.update(TICK)
    assert quiet.properties["awake"] is False, (
        "a loudness-0.25 noise reaches 250 units; the monster is %.0f away"
        % distance)

    logic._noise_events.clear()
    logic.emit_noise((0, 96, 0), source="gunfire", loudness=1.0)   # reach 1000
    ai.update(TICK)
    assert quiet.properties["awake"] is True


# ---------------------------------------------------------------------------
# Target acquisition and loss
# ---------------------------------------------------------------------------

def test_entering_sight_range_fires_onseeplayer_once(monster_factory, ai_world,
                                                     flat_ground, io_manager):
    monster = monster_factory("watcher", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    logic.io_manager = io_manager

    ai.update(TICK)
    ai.update(TICK)

    fired = [call for call in io_manager.fired if call[1] == "OnSeePlayer"]
    assert len(fired) == 1, (
        "OnSeePlayer fired %d times across two ticks in sight; it is an "
        "edge, not a level" % len(fired))
    assert _state(ai, monster)["in_sight"] is True


def test_leaving_sight_range_fires_onlostplayer(monster_factory, ai_world,
                                                flat_ground, io_manager):
    monster = monster_factory("watcher", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    logic.io_manager = io_manager
    ai.update(TICK)

    monster.pos = [MONSTER_SIGHT_RANGE + 500, 96, 0]
    ai.update(TICK)

    outputs = [call[1] for call in io_manager.fired]
    assert "OnLostPlayer" in outputs, (
        "moving to %.0f units (sight range %d) did not fire OnLostPlayer; "
        "outputs were %s"
        % (monster.pos[0], MONSTER_SIGHT_RANGE, outputs))
    assert _state(ai, monster)["in_sight"] is False
    assert monster.properties["is_shooting"] is False


def test_a_named_target_override_takes_priority_over_the_player(
        monster_factory, path_node_factory, ai_world, flat_ground):
    marker = path_node_factory("waypoint", (400, 96, 0))
    monster = monster_factory("seeker", (0, 96, 0), target_name="waypoint")
    ai, logic = ai_world(brushes=flat_ground, things=[monster, marker],
                         player_pos=(-400.0, 0.0, 0.0))
    logic.rebuild_name_cache()

    start_x = monster.pos[0]
    for _ in range(10):
        ai.update(TICK)

    assert monster.pos[0] > start_x, (
        "'%s' should walk toward its named target at x=400, but moved from "
        "%.1f to %.1f (the player is at x=-400)"
        % (monster.name, start_x, monster.pos[0]))


def test_an_unresolvable_target_name_is_cleared_and_the_player_resumed(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("seeker", (400, 96, 0), target_name="does_not_exist")
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ai.update(TICK)

    assert "target_name" not in monster.properties, (
        "a target_name that names no entity must be cleared, not retried every "
        "tick; it is still %r" % monster.properties.get("target_name"))


def test_notarget_stops_the_chase_without_stopping_gravity(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("chaser", (400, 500, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    logic.notarget = True

    before_x = monster.pos[0]
    for _ in range(10):
        ai.update(TICK)

    assert monster.pos[0] == pytest.approx(before_x), (
        "notarget is on, but '%s' moved from x=%.1f to x=%.1f"
        % (monster.name, before_x, monster.pos[0]))
    assert monster.pos[1] < 500.0, (
        "notarget must not switch gravity off; '%s' is still at y=%.1f"
        % (monster.name, monster.pos[1]))
    assert monster.properties["is_shooting"] is False


# ---------------------------------------------------------------------------
# Movement
# ---------------------------------------------------------------------------

def test_a_monster_closes_on_the_player_at_the_configured_speed(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("chaser", (600, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ai.update(TICK)

    travelled = 600.0 - monster.pos[0]
    expected = MONSTER_MOVE_SPEED * TICK
    assert travelled == pytest.approx(expected, rel=1e-3), (
        "'%s' moved %.4f units in one %.4fs tick; at %.1f units/s it should "
        "have moved %.4f" % (monster.name, travelled, TICK, MONSTER_MOVE_SPEED,
                             expected))


def test_a_ground_monster_does_not_climb_toward_a_higher_player(
        monster_factory, ai_world, flat_ground):
    """Ground movement is flattened to XZ; only gravity moves it vertically."""
    monster = monster_factory("chaser", (600, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster],
                         player_pos=(0.0, 400.0, 0.0))
    start_y = monster.pos[1]

    for _ in range(5):
        ai.update(TICK)

    assert monster.pos[1] == pytest.approx(start_y, abs=1e-6), (
        "a ground monster rose from y=%.2f to y=%.2f chasing a player 400 "
        "units up" % (start_y, monster.pos[1]))
    assert monster.pos[0] < 600.0, "it should still have closed horizontally"


def test_a_flying_monster_climbs_toward_a_higher_player(monster_factory, ai_world):
    monster = monster_factory("flier", (600, 96, 0), monster_type="flying")
    ai, logic = ai_world(things=[monster], player_pos=(0.0, 400.0, 0.0))

    for _ in range(5):
        ai.update(TICK)

    assert monster.pos[1] > 96.0, (
        "a flying monster chasing a player at y=400 stayed at y=%.2f"
        % monster.pos[1])


def test_a_monster_stops_at_the_stop_distance(monster_factory, ai_world):
    """The stop distance is measured in 3D, so the test keeps Y out of it.

    A flying monster is used because a ground monster is pulled to standing
    height by gravity, which alone puts it past the 85-unit stop distance.
    """
    monster = monster_factory("hoverer", (MONSTER_STOP_DISTANCE - 5.0, 0, 0),
                              monster_type="flying")
    ai, logic = ai_world(things=[monster], player_pos=(0.0, 0.0, 0.0))
    before = list(monster.pos)

    for _ in range(10):
        ai.update(TICK)

    assert monster.pos[0] == pytest.approx(before[0]), (
        "'%s' is %.1f units away, inside the %.0f-unit stop distance, but "
        "moved from x=%.2f to x=%.2f"
        % (monster.name, MONSTER_STOP_DISTANCE - 5.0, MONSTER_STOP_DISTANCE,
           before[0], monster.pos[0]))


def test_a_wall_blocks_a_monster_and_it_slides_along_it(monster_factory, ai_world,
                                                        flat_ground):
    wall = box_brush("wall", (200, 96, 0), (32, 256, 1024))
    monster = monster_factory("chaser", (400, 96, 300))
    ai, logic = ai_world(brushes=flat_ground + [wall], things=[monster])

    for _ in range(60):
        ai.update(TICK)

    assert monster.pos[0] > wall["pos"][0], (
        "'%s' walked through the wall at x=%.0f; it is now at x=%.2f"
        % (monster.name, wall["pos"][0], monster.pos[0]))
    assert abs(monster.pos[2]) < 300.0, (
        "'%s' should have slid along the wall toward z=0, but z=%.2f"
        % (monster.name, monster.pos[2]))


def test_a_monster_boxed_in_on_both_axes_does_not_move(monster_factory, ai_world):
    """Fully blocked means no movement, not a jitter or a tunnel."""
    box = [
        box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
        box_brush("w_east", (40, 96, 0), (16, 256, 256)),
        box_brush("w_west", (-40, 96, 0), (16, 256, 256)),
        box_brush("w_north", (0, 96, 40), (256, 256, 16)),
        box_brush("w_south", (0, 96, -40), (256, 256, 16)),
    ]
    monster = monster_factory("boxed", (0, 96, 0))
    ai, logic = ai_world(brushes=box, things=[monster],
                         player_pos=(500.0, 0.0, 500.0))
    before = list(monster.pos)

    for _ in range(20):
        ai.update(TICK)

    assert monster.pos[0] == pytest.approx(before[0], abs=1e-6)
    assert monster.pos[2] == pytest.approx(before[2], abs=1e-6)


def test_gravity_pulls_a_monster_down_to_the_floor(monster_factory, ai_world,
                                                   flat_ground):
    monster = monster_factory("faller", (0, 1000, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster],
                         player_pos=(0.0, 0.0, 0.0))

    for _ in range(300):        # 10 simulated seconds
        ai.update(TICK)

    floor_top = 0.0             # flat_ground spans y -32..0
    expected_centre = floor_top + monster.properties["sprite_height"] / 2.0
    assert monster.pos[1] == pytest.approx(expected_centre, abs=2.0), (
        "'%s' settled at y=%.2f; standing on a floor whose top is y=%.1f with "
        "a %d-unit sprite it should rest at y=%.1f"
        % (monster.name, monster.pos[1], floor_top,
           monster.properties["sprite_height"], expected_centre))


def test_a_flying_monster_is_not_pulled_down(monster_factory, ai_world, flat_ground):
    monster = monster_factory("hoverer", (0, 600, 0), monster_type="flying")
    ai, logic = ai_world(brushes=flat_ground, things=[monster],
                         player_pos=(0.0, 600.0, 0.0))
    for _ in range(30):
        ai.update(TICK)
    assert monster.pos[1] == pytest.approx(600.0, abs=1.0), (
        "a flying monster fell to y=%.2f" % monster.pos[1])


# ---------------------------------------------------------------------------
# Line of sight
# ---------------------------------------------------------------------------

def test_a_monster_behind_a_wall_does_not_shoot(monster_factory, ai_world):
    brushes = [
        box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
        box_brush("wall", (250, 200, 0), (32, 400, 1024)),
    ]
    monster = monster_factory("blocked", (500, 96, 0))
    ai, logic = ai_world(brushes=brushes, things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 5):
        ai.update(TICK)

    assert logic.damage_applied == [], (
        "'%s' shot through a 400-unit-tall wall; damage dealt: %s"
        % (monster.name, logic.damage_applied))


def test_a_monster_with_clear_sight_does_shoot(monster_factory, ai_world,
                                               flat_ground):
    monster = monster_factory("shooter", (500, 96, 0), damage=13)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    assert logic.damage_applied == [13], (
        "expected exactly one 13-point hit after %.2fs; got %s"
        % (MONSTER_SHOOT_INTERVAL, logic.damage_applied))


def test_line_of_sight_uses_the_spatial_grid_when_one_is_set(monster_factory,
                                                             ai_world, flat_ground):
    """The fallback full-brush scan must not be what answers in play mode."""
    monster = monster_factory("shooter", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    calls = []
    real = ai._grid.has_line_of_sight

    def _counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    ai._grid.has_line_of_sight = _counting
    ai.update(TICK)
    assert calls, ("MonsterAI did not route line of sight through the spatial "
                   "grid; it fell back to the O(all brushes) scan")


# ---------------------------------------------------------------------------
# Attack cooldown and timing
# ---------------------------------------------------------------------------

def test_attacks_are_spaced_by_the_shoot_interval(monster_factory, ai_world,
                                                  flat_ground):
    monster = monster_factory("shooter", (500, 96, 0), damage=7)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ticks_per_interval = int(round(MONSTER_SHOOT_INTERVAL / TICK))
    for _ in range(ticks_per_interval * 3 + 3):
        ai.update(TICK)

    assert logic.damage_applied == [7, 7, 7], (
        "over 3 shoot intervals expected 3 hits of 7 damage, got %s"
        % (logic.damage_applied,))


def test_the_shoot_animation_flag_clears_after_its_time(monster_factory, ai_world,
                                                        flat_ground):
    monster = monster_factory("shooter", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)
    assert monster.properties["is_shooting"] is True, \
        "the shoot sprite should be up immediately after firing"

    for _ in range(int(MONSTER_SHOOT_ANIM_TIME / TICK) + 2):
        ai.update(TICK)
    assert monster.properties["is_shooting"] is False, (
        "the shoot sprite is still up %.2fs after firing (anim time is %.2fs)"
        % (MONSTER_SHOOT_ANIM_TIME + 2 * TICK, MONSTER_SHOOT_ANIM_TIME))


def test_a_blocked_shot_retries_soon_rather_than_waiting_a_full_interval(
        monster_factory, ai_world):
    brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
               box_brush("wall", (250, 200, 0), (32, 400, 1024))]
    monster = monster_factory("blocked", (500, 96, 0))
    ai, logic = ai_world(brushes=brushes, things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    timer = _state(ai, monster)["shoot_timer"]
    assert timer <= 0.1 + 1e-6, (
        "a monster whose shot was blocked should re-check within 0.1s; its "
        "shoot_timer is %.4f" % timer)


def test_firing_queues_the_type_specific_shoot_sound(monster_factory, ai_world,
                                                     flat_ground):
    monster = monster_factory("shooter", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)
    assert len(logic.game_state.sounds) == 1, (
        "one shot should queue one sound, got %s" % (logic.game_state.sounds,))
    assert logic.game_state.sounds[0]["entity_id"] == id(monster)


def test_firing_fires_the_onattack_output(monster_factory, ai_world, flat_ground,
                                          io_manager):
    monster = monster_factory("shooter", (500, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    logic.io_manager = io_manager
    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)
    assert ("shooter", "OnAttack") in [(c[0].name, c[1]) for c in io_manager.fired], (
        "OnAttack was not fired; outputs were %s"
        % [c[1] for c in io_manager.fired])


# ---------------------------------------------------------------------------
# Projectiles (flying monsters)
# ---------------------------------------------------------------------------

def test_a_distant_flying_monster_spawns_a_projectile(monster_factory, ai_world):
    monster = monster_factory("flier", (600, 60, 0), monster_type="flying",
                              damage=9)
    ai, logic = ai_world(things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    assert len(logic._monster_projectiles) == 1, (
        "a flying monster %.0f units away should fire a projectile, not a "
        "hitscan; projectiles=%d damage=%s"
        % (_distance(monster.pos, logic.player.pos),
           len(logic._monster_projectiles), logic.damage_applied))
    projectile = logic._monster_projectiles[0]
    assert projectile["damage"] == 9
    assert projectile["owner_id"] == id(monster)
    # It must be aimed at the player, i.e. travelling in -X.
    assert projectile["vel"][0] < 0, (
        "projectile velocity %s does not point back toward the player at the "
        "origin" % (projectile["vel"],))


def test_a_flying_monster_in_biting_range_bites_instead(monster_factory, ai_world):
    from engine.monster_constants import MONSTER_BITE_DAMAGE_MULT, MONSTER_BITE_DISTANCE
    monster = monster_factory("biter", (MONSTER_BITE_DISTANCE - 5.0, 0, 0),
                              monster_type="flying", damage=10)
    ai, logic = ai_world(things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    assert logic._monster_projectiles == [], \
        "a biting flier should not also spawn a projectile"
    assert logic.damage_applied == [int(10 * MONSTER_BITE_DAMAGE_MULT)], (
        "a bite does %sx damage; expected %d, got %s"
        % (MONSTER_BITE_DAMAGE_MULT, int(10 * MONSTER_BITE_DAMAGE_MULT),
           logic.damage_applied))


# ---------------------------------------------------------------------------
# Death and removal
# ---------------------------------------------------------------------------

def test_a_dead_monster_stops_attacking_and_falls_to_the_floor(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("corpse", (500, 400, 0), dead=True)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    for _ in range(200):
        ai.update(TICK)

    assert logic.damage_applied == [], "a dead monster dealt damage"
    assert "is_shooting" not in monster.properties
    expected = monster.properties["sprite_height"] / 2.0
    assert monster.pos[1] == pytest.approx(expected, abs=2.0), (
        "the corpse settled at y=%.2f, expected y=%.1f on the floor"
        % (monster.pos[1], expected))


def test_the_kill_input_marks_a_monster_dead_and_is_consumed(
        monster_factory, ai_world, flat_ground):
    monster = monster_factory("victim", (500, 96, 0))
    monster.properties["_kill"] = True
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    ai.update(TICK)

    assert monster.properties["dead"] is True
    assert "_kill" not in monster.properties, (
        "the kill request must be consumed, not left to re-fire every tick")


def test_a_hidden_monster_is_skipped_entirely(monster_factory, ai_world,
                                              flat_ground):
    monster = monster_factory("ghost", (200, 96, 0), hidden=True)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    before = list(monster.pos)

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 5):
        ai.update(TICK)

    assert monster.pos == before, "a hidden monster moved"
    assert logic.damage_applied == [], "a hidden monster attacked"


def test_a_disabled_monster_is_skipped_entirely(monster_factory, ai_world,
                                                flat_ground):
    monster = monster_factory("off", (200, 96, 0), disabled=True)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])
    before = list(monster.pos)
    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 5):
        ai.update(TICK)
    assert monster.pos == before
    assert logic.damage_applied == []


def test_the_player_dying_is_reported_once(monster_factory, ai_world, flat_ground):
    monster = monster_factory("shooter", (300, 96, 0), damage=200)
    ai, logic = ai_world(brushes=flat_ground, things=[monster])

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) * 3):
        ai.update(TICK)

    assert logic.player_dead is True, (
        "player health is %d after taking %s damage but player_dead is False"
        % (logic.player_health, logic.damage_applied))
    assert len(logic.damage_applied) == 1, (
        "the AI kept attacking a dead player: %s" % (logic.damage_applied,))


# ---------------------------------------------------------------------------
# Several monsters at once
# ---------------------------------------------------------------------------

def test_several_monsters_keep_independent_state(monster_factory, ai_world,
                                                 flat_ground):
    near = monster_factory("near", (300, 96, 0))
    far = monster_factory("far", (MONSTER_SIGHT_RANGE + 400, 96, 0))
    ai, logic = ai_world(brushes=flat_ground, things=[near, far])

    ai.update(TICK)

    assert _state(ai, near)["in_sight"] is True, "'near' is 300 units away"
    assert _state(ai, far)["in_sight"] is False, (
        "'far' is %.0f units away (sight range %d) but is marked in sight"
        % (MONSTER_SIGHT_RANGE + 400, MONSTER_SIGHT_RANGE))
    assert len(ai.monster_states) == 2


def test_several_monsters_targeting_one_player_each_deal_their_own_damage(
        monster_factory, ai_world, flat_ground):
    monsters = [monster_factory("m%d" % i, (300 + i * 20, 96, 0), damage=i + 1)
                for i in range(4)]
    ai, logic = ai_world(brushes=flat_ground, things=monsters)

    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    assert sorted(logic.damage_applied) == [1, 2, 3, 4], (
        "four monsters with damage 1..4 should each land one hit; got %s"
        % (logic.damage_applied,))


def test_a_monster_removed_from_the_world_leaves_its_state_behind_harmlessly(
        monster_factory, ai_world, flat_ground):
    """State is keyed by ``id(thing)``; a removed monster must not be updated.

    The stale entry itself is expected - ``monster_states`` is cleared on
    play-mode transitions - but it must not cause the removed monster to act.
    """
    a = monster_factory("a", (300, 96, 0), damage=5)
    b = monster_factory("b", (320, 96, 0), damage=6)
    ai, logic = ai_world(brushes=flat_ground, things=[a, b])
    ai.update(TICK)

    logic.things.remove(b)
    logic._monster_things = [a]
    for _ in range(int(MONSTER_SHOOT_INTERVAL / TICK) + 2):
        ai.update(TICK)

    assert logic.damage_applied == [5], (
        "the removed monster 'b' still attacked: damage log %s"
        % (logic.damage_applied,))
