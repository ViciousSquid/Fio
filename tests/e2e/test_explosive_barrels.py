"""Explosive Props (barrels), in a real Play session.

A Prop with ``explosive`` on takes damage from shots, projectiles and other
explosions; once it has taken its ``explosive_health`` it explodes: the model
is removed (hidden, its collision gone), an EXPLOSION Effect plays where it
stood, everything within ``explosion_radius`` that the blast can see takes
``explosion_damage`` -- the player, monsters, and other explosive Props, which
go off in turn -- and the player, monsters and physics Props are pushed away.
Stopping Play puts the world back as authored.

The clock is frozen and stepped, so the chain's delays are exact.
"""

import time

import glm
import pytest

pytest.importorskip("PyQt5")

from types import SimpleNamespace                                    # noqa: E402

from editor.things import Monster, PlayerStart                       # noqa: E402
from engine.explosions import ExplosionRuntime                       # noqa: E402
from engine.prop_entity import Prop                                  # noqa: E402
from tests.helpers.worlds import box_brush, level_data, make_thing, room  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


def _barrel(name, x, z, **props):
    settings = {"model_path": "Oil_Drum.obj", "explosive": True,
                "explosive_health": 20, "explosion_damage": 40,
                "explosion_radius": 200.0, "explosion_force": 400.0}
    settings.update(props)
    return make_thing(Prop, name, (x, 0.0, z), **settings)


@pytest.fixture
def clock(monkeypatch):
    import engine.entity_table as et
    now = [et._CLOCK_ORIGIN + 1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    return now


def _play(fio_session, clock, things, brushes=None):
    level = level_data(brushes=brushes if brushes is not None else room(size=1600.0),
                       things=[make_thing(PlayerStart, "spawn", (500.0, 40.0, 500.0))]
                       + list(things))
    session = fio_session(level).start_play()
    session.clock = clock
    _step(session, 2)
    return session


def _step(session, ticks=1):
    for _ in range(ticks):
        session.clock[0] += TICK
        session.step(1)


def _named(session, name):
    return next(t for t in session.state.things if t.properties.get("name") == name)


def _shoot(session, prop, damage=12, origin=(0.0, 0.0, 300.0)):
    logic = session.logic
    centre = glm.vec3(*logic.explosion_runtime.centre(prop))
    start = glm.vec3(origin[0], centre.y, origin[2])
    weapon = SimpleNamespace(damage=damage, range=4000.0, mode="hitscan")
    return logic.combat_runtime._fire_rays(weapon, start, [glm.normalize(centre - start)])


def _exploded(session, prop):
    return id(prop) in session.logic.explosion_runtime.exploded


def test_shots_add_up_until_the_barrel_explodes(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0)])
    barrel = _named(session, "b1")
    _shoot(session, barrel)
    _step(session)
    assert not _exploded(session, barrel)
    assert session.logic.explosion_runtime.damage_taken[id(barrel)] == 12
    _shoot(session, barrel)                       # 24 >= 20
    _step(session)
    assert _exploded(session, barrel)
    assert barrel.properties["hidden"] is True
    fireball = _named(session, "b1_explosion")
    assert fireball in session.logic.session_runtime.session_things
    # Its collision went with it: no model box of it is left.
    assert not any(b.get("_physics_entity") is barrel
                   for b in session.logic.collision_runtime._model_collision_brushes)


def test_a_projectile_damages_a_barrel(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0, explosive_health=5)])
    barrel = _named(session, "b1")
    cx, cy, cz = session.logic.explosion_runtime.centre(barrel)
    session.logic.combat_runtime._add_monster_projectile(
        [cx + 60.0, cy, cz], [-900.0, 0.0, 0.0], 0, 10.0, 2.0)
    _step(session, 6)
    assert _exploded(session, barrel)


def test_the_blast_damages_and_pushes_what_it_can_see(fio_session, clock):
    walls = room(size=1600.0) + [box_brush("screen", (0.0, 128.0, -150.0), (400.0, 256.0, 32.0))]
    session = _play(fio_session, clock, [
        _barrel("b1", 0.0, 0.0),
        make_thing(Monster, "near", (0.0, 64.0, 120.0), health=100),
        make_thing(Monster, "hidden_by_wall", (0.0, 64.0, -260.0), health=100),
    ], brushes=walls)
    logic = session.logic
    player = logic.player_runtime.player
    player.pos = glm.vec3(130.0, player.pos.y, 0.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    near, shielded = _named(session, "near"), _named(session, "hidden_by_wall")
    start = list(near.pos)

    logic.explosion_runtime.explode_prop(_named(session, "b1"))

    assert logic.player_runtime.player_health == 60
    assert player.velocity.x > 50.0 and player.velocity.y > 0.0     # shoved away, lifted
    assert logic.player_runtime.damage_marks                        # the HUD shows where
    assert near.properties["health"] == 60
    assert shielded.properties["health"] == 100                     # the wall stopped it
    _step(session, 12)
    assert near.pos[2] > start[2] + 10.0                            # pushed away from it


def test_barrels_set_each_other_off_in_turn(fio_session, clock):
    session = _play(fio_session, clock, [
        _barrel("b1", 0.0, 0.0), _barrel("b2", 150.0, 0.0),
        _barrel("b3", 600.0, 0.0)])                                 # out of reach
    b1, b2, b3 = (_named(session, n) for n in ("b1", "b2", "b3"))
    session.logic.explosion_runtime.explode_prop(b1)
    assert not _exploded(session, b2)                               # not at once...
    _step(session, int(ExplosionRuntime.CHAIN_DELAY / TICK) + 2)
    assert _exploded(session, b2)                                   # ...but soon after
    _step(session, 30)
    assert not _exploded(session, b3)


def test_stopping_play_puts_the_world_back(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0),
                                         _barrel("b2", 150.0, 0.0)])
    count = len(session.state.things)
    b1, b2 = _named(session, "b1"), _named(session, "b2")
    session.logic.explosion_runtime.explode_prop(b1)
    _step(session, 20)
    assert _exploded(session, b2) and len(session.state.things) == count + 2
    session.stop_play()
    assert b1.properties["hidden"] is False and b2.properties["hidden"] is False
    assert len(session.state.things) == count                       # fireballs gone
    session.start_play()
    _step(session, 2)
    assert not session.logic.explosion_runtime.exploded
    assert len(session.logic.explosion_runtime.explosives()) == 2


def test_a_prop_that_is_not_explosive_ignores_damage(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("plain", 0.0, 0.0, explosive=False)])
    plain = _named(session, "plain")
    assert session.logic.explosion_runtime.explosives() == []
    _shoot(session, plain, damage=500)
    _step(session)
    assert not _exploded(session, plain) and not plain.properties.get("hidden")


def test_the_damage_and_explode_inputs(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0),
                                         _barrel("b2", 700.0, 0.0)])
    io = session.logic.io_manager
    io._execute_input("b1", "Damage", "15", "test")
    _step(session)
    assert not _exploded(session, _named(session, "b1"))
    io._execute_input("b1", "Damage", "5", "test")
    _step(session)
    assert _exploded(session, _named(session, "b1"))
    io._execute_input("b2", "Explode", "", "test")
    assert _exploded(session, _named(session, "b2"))


def test_physics_props_are_pushed(fio_session, clock):
    session = _play(fio_session, clock, [
        _barrel("b1", 0.0, 0.0),
        make_thing(Prop, "crate", (90.0, 0.0, 0.0), model_path="Oil_Drum.obj",
                   physics_enabled=True)])
    crate = _named(session, "crate")
    if session.logic.session_runtime.physics_world is None:
        pytest.skip("physics is off")
    before = float(crate.pos[0])
    session.logic.explosion_runtime.explode_prop(_named(session, "b1"))
    _step(session, 20)
    assert float(crate.pos[0]) > before + 2.0


def test_a_map_without_explosives_pays_nothing(fio_session, clock):
    """Every entry point returns at its first check."""
    session = _play(fio_session, clock, [])
    runtime = session.logic.explosion_runtime
    assert runtime.explosives() == []
    assert runtime.trace((0, 0, 0), (1, 0, 0), 100.0) is None
    assert runtime.point_hit(0, 0, 0) is None
    runtime.update()
    assert not runtime.pending


def test_a_pushed_monster_stops_at_a_wall(fio_session, clock):
    from engine.monster_constants import MONSTER_WALL_MARGIN
    session = _play(fio_session, clock, [
        make_thing(Monster, "grunt", (500.0, 64.0, 0.0), health=100)])
    grunt = _named(session, "grunt")
    ai = session.logic.monster_ai
    with session.logic.session_runtime.monster_lock:
        ai.knock_back(grunt, 1.0, 0.0, 3000.0)       # hard, into the east wall
    _step(session, 60)
    assert not ai._knockback                          # the shove is over
    x, y, z = grunt.pos
    assert not ai._monster_overlaps_wall(x, y, z, MONSTER_WALL_MARGIN)
    assert 500.0 < x <= 800.0 - MONSTER_WALL_MARGIN + 1e-3


def test_a_prop_with_physics_off_is_never_moved(fio_session, clock):
    session = _play(fio_session, clock, [
        _barrel("b1", 0.0, 0.0),
        make_thing(Prop, "statue", (90.0, 0.0, 0.0), model_path="Oil_Drum.obj",
                   physics_enabled=False)])
    statue = _named(session, "statue")
    session.logic.explosion_runtime.explode_prop(_named(session, "b1"))
    _step(session, 20)
    assert list(statue.pos) == [90.0, 0.0, 0.0]
    assert statue.properties["physics_enabled"] is False


def test_with_physics_off_in_settings_nothing_is_pushed(fio_session, clock):
    level = level_data(brushes=room(size=1600.0), things=[
        make_thing(PlayerStart, "spawn", (500.0, 40.0, 500.0)),
        _barrel("b1", 0.0, 0.0),
        make_thing(Prop, "crate", (90.0, 0.0, 0.0), model_path="Oil_Drum.obj",
                   physics_enabled=True)])
    session = fio_session(level)
    session.clock = clock
    session.window.config.set("Settings", "physics", "False")
    session.start_play()
    _step(session, 2)
    assert session.logic.session_runtime.physics_world is None
    crate = _named(session, "crate")
    session.logic.explosion_runtime.explode_prop(_named(session, "b1"))
    _step(session, 20)
    assert list(crate.pos) == [90.0, 0.0, 0.0]
    assert crate.properties["physics_enabled"] is True        # left as authored


def test_a_barrel_becomes_an_effect_explosion_where_its_model_was(fio_session, clock):
    """The model is removed and, at the same moment, an EXPLOSION Effect is
    spawned where it was, carrying the barrel's settings, sized to cover the
    model and opening partway in: the Effect does the exploding."""
    from engine.effect_entity import Effect
    from engine.explosions import BARREL_COVER, BARREL_SKIP
    session = _play(fio_session, clock, [_barrel("b1", 40.0, -30.0, explosion_damage=33,
                                                 explosion_radius=150.0, explosion_force=250.0,
                                                 explosion_volume=0.5)])
    runtime = session.logic.explosion_runtime
    barrel = _named(session, "b1")
    lo, hi = runtime.box(barrel)
    centre = runtime.centre(barrel)
    effect = runtime.explode_prop(barrel)
    assert isinstance(effect, Effect) and barrel.properties["hidden"] is True
    assert effect in session.state.things
    assert list(effect.pos) == pytest.approx([centre[0], lo[1], centre[2]])
    p = effect.properties
    assert p["effect_type"] == "EXPLOSION"
    assert (p["explosion_damage"], p["explosion_radius"], p["explosion_force"],
            p["explosion_volume"]) == (33, 150.0, 250.0, 0.5)
    assert p["height"] >= BARREL_COVER * (hi[1] - lo[1]) - 1e-6
    assert p["explosion_skip"] == BARREL_SKIP
    # It is playing: the Effect's own explosion.
    _step(session)
    with session.render_state() as frame:
        table = frame.entity_table
        slot = table.slot_of_id[p["id"]]
        assert bool(table.effect_alive[slot]) and int(table.effect_type[slot]) == 1
        assert table.effect_skip[slot] == pytest.approx(BARREL_SKIP)


def test_a_physics_barrel_explodes_where_it_has_rolled_to(fio_session, clock):
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0)])
    barrel = _named(session, "b1")
    barrel.pos = [210.0, 0.0, -75.0]                  # knocked across the room
    effect = session.logic.explosion_runtime.explode_prop(barrel)
    assert effect.pos[0] == pytest.approx(210.0) and effect.pos[2] == pytest.approx(-75.0)


def test_an_effect_is_harmless_unless_given_a_blast(fio_session, clock):
    from engine.effect_entity import Effect
    session = _play(fio_session, clock, [
        make_thing(Effect, "torch", (0.0, 30.0, 0.0), effect_type="FIRE"),
        make_thing(Effect, "mine", (0.0, 0.0, 400.0), effect_type="EXPLOSION",
                   explosion_damage=25, explosion_radius=150.0)])
    logic = session.logic
    player = logic.player_runtime.player
    player.pos = glm.vec3(60.0, player.pos.y, 0.0)
    logic.io_manager._execute_input("torch", "Explode", "", "test")
    assert logic.player_runtime.player_health == 100            # no blast set
    player.pos = glm.vec3(60.0, player.pos.y, 400.0)
    logic.io_manager._execute_input("mine", "Explode", "", "test")
    assert logic.player_runtime.player_health == 75             # its own blast


def _hits(session, prop, start, direction):
    logic = session.logic
    before = logic.explosion_runtime.damage_taken.get(id(prop), 0.0)
    weapon = SimpleNamespace(damage=1, range=4000.0, mode="hitscan")
    logic.combat_runtime._fire_rays(weapon, glm.vec3(*start), [glm.normalize(glm.vec3(*direction))])
    return logic.explosion_runtime.damage_taken.get(id(prop), 0.0) > before


def test_a_shot_anywhere_on_the_barrel_hits_it(fio_session, clock):
    """Not only at its middle: its edges and top and bottom too."""
    session = _play(fio_session, clock, [_barrel("b1", 0.0, 0.0, explosive_health=9999)])
    barrel = _named(session, "b1")
    lo, hi = session.logic.explosion_runtime.box(barrel)
    for x in (lo[0] + 2.0, 0.0, hi[0] - 2.0):
        for y in (lo[1] + 2.0, (lo[1] + hi[1]) * 0.5, hi[1] - 2.0):
            assert _hits(session, barrel, (x, y, 300.0), (0.0, 0.0, -1.0)), (x, y)
    assert not _hits(session, barrel, (hi[0] + 4.0, 30.0, 300.0), (0.0, 0.0, -1.0))
    assert not _hits(session, barrel, (0.0, hi[1] + 4.0, 300.0), (0.0, 0.0, -1.0))


def test_a_barrel_lying_on_its_side_is_hit_along_its_whole_length(fio_session, clock):
    """Knocked over (physics writes its rotation), a barrel is a long low
    shape: a shot at either end hits it, a shot where it no longer stands
    passes by."""
    lying = _barrel("b1", 0.0, 0.0, explosive_health=9999, rotation=[90.0, 0.0, 0.0])
    lying.pos = [0.0, 23.0, 0.0]            # its round side on the floor
    session = _play(fio_session, clock, [lying])
    barrel = _named(session, "b1")
    ex = session.logic.explosion_runtime
    lo, hi = ex.box(barrel)
    # Turned by 90 degrees about X, the drum's height lies along +Z.
    assert hi[2] - lo[2] > 60.0 and hi[1] - lo[1] < 50.0
    middle = (lo[1] + hi[1]) * 0.5
    for z in (lo[2] + 3.0, (lo[2] + hi[2]) * 0.5, hi[2] - 3.0):
        assert _hits(session, barrel, (300.0, middle, z), (-1.0, 0.0, 0.0)), z
    # Above where it lies, though an upright barrel would be there.
    assert not _hits(session, barrel, (300.0, hi[1] + 15.0, 10.0), (-1.0, 0.0, 0.0))
    assert ex.point_hit(0.0, middle, hi[2] - 3.0) is barrel
    assert ex.point_hit(0.0, hi[1] + 15.0, 10.0) is None


def _grid_of_barrels(n, spacing=110.0, **props):
    side = int(n ** 0.5)
    return [_barrel("g%d" % i, (i % side) * spacing - 300.0, (i // side) * spacing - 300.0,
                    **props)
            for i in range(n)]


def test_a_chain_reaction_never_rescans_the_world(fio_session, clock):
    """The standing barrels are listed once; a barrel that goes leaves the
    list itself, and its fireball (a session Thing) does not empty the cache."""
    session = _play(fio_session, clock, _grid_of_barrels(25, explosion_radius=260.0))
    runtime = session.logic.explosion_runtime
    assert len(runtime.explosives()) == 25
    scans = runtime.scans
    runtime.explode_prop(_named(session, "g0"))
    _step(session, 120)
    assert len(runtime.exploded) == 25 and runtime.explosives() == []
    # Was one full scan of the things per explosion (25).
    assert runtime.scans == scans


def test_a_chain_reaction_tests_each_barrels_sight_until_it_is_caught(fio_session, clock):
    """A barrel already waiting to go off is not looked at again by every
    later blast that reaches it."""
    session = _play(fio_session, clock, _grid_of_barrels(25, explosion_radius=260.0))
    runtime = session.logic.explosion_runtime
    runtime.explode_prop(_named(session, "g0"))
    _step(session, 120)
    assert len(runtime.exploded) == 25
    # 25 blasts each reaching ~10 neighbours used to test ~150 lines of sight;
    # now a barrel is tested only by blasts before the one that caught it.
    assert runtime.sight_tests <= 60, runtime.sight_tests


def test_out_of_range_blast_settings_are_clamped(fio_session, clock):
    """``"inf"`` and absurd values neither fault the tick nor reach the
    radius queries and physics as they are: a value that is not a finite
    number is the default, one past its maximum is the maximum."""
    from engine.explosions import BLAST_MAXIMUMS, EXPLOSIVE_DEFAULTS
    session = _play(fio_session, clock, [
        _barrel("b1", 0.0, 0.0, explosion_radius="inf", explosion_force=1e12,
                explosion_damage=float("inf")),
        _barrel("b2", 150.0, 0.0)])
    runtime = session.logic.explosion_runtime
    runtime.explode_prop(_named(session, "b1"))         # used to raise OverflowError
    fireball = _named(session, "b1_explosion")
    assert fireball.properties["explosion_radius"] == EXPLOSIVE_DEFAULTS["explosion_radius"]
    assert fireball.properties["explosion_damage"] == EXPLOSIVE_DEFAULTS["explosion_damage"]
    assert fireball.properties["explosion_force"] == BLAST_MAXIMUMS["explosion_force"]
    _step(session, int(ExplosionRuntime.CHAIN_DELAY / TICK) + 2)
    assert _exploded(session, _named(session, "b2"))
