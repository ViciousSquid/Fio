"""A shot's pellets are simultaneous: they trace one instant of the world.

The contract, as the maintainer set it:

* every pellet of one shot traces against the same world -- the collision
  brushes and one snapshot of the monsters, taken under a single
  ``monster_lock`` acquisition;
* the effects (damage, death, I/O outputs, bullet marks) are applied after
  tracing, pellet by pellet in scatter order;
* a monster an earlier pellet's damage killed is passed over by a later
  pellet's pending hit, which goes on to what it met behind;
* I/O a pellet's hit fires reaches only the next shot, never the other
  pellets of its own.

Before this, pellets traced one after another and an earlier pellet's I/O
could change what a later one hit. These tests pin the new behaviour; that the
dense passes answer exactly as the per-brush and per-monster walks did is
``test_hitscan_dense.py``'s business.
"""

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor import io_system as io                        # noqa: E402
from editor.editor_state import EditorState               # noqa: E402
from editor.io_system import OutputConnection             # noqa: E402
from editor.things import Monster, PlayerStart            # noqa: E402
from engine.items import DEFAULT_DEFINITIONS              # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

#: The player's eye: Player(0, 0) at y=40 with camera_height 40, facing +z.
EYE_Y = 80.0

#: Five pellets in a 1-degree cone: at a few hundred units every pellet passes
#: within a few units of the eye ray, well inside any monster's hit sphere.
PELLETS = 5


def _shotgun(damage=10.0, spread=1.0):
    definition = dict(DEFAULT_DEFINITIONS["custom2"])
    definition["weapon"] = {**definition.get("weapon", {}),
                            "mode": "hitscan", "damage": damage,
                            "pellets": PELLETS, "spread": spread, "range": 5000.0,
                            "ammo": 0, "ammo_per_shot": 0, "cooldown": 0.0}
    return {"custom2": definition}


def _monster_ahead(name, distance, **props):
    """A monster whose hitscan sphere is centred on the eye ray *distance* ahead."""
    props.setdefault("health", 100)
    return make_thing(Monster, name, (0, EYE_Y - 128 * 0.45, distance), **props)


@pytest.fixture
def world():
    made = []

    def _build(things=(), brushes=(), damage=10.0, spread=1.0):
        state = EditorState()
        state.brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096)), *brushes]
        state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0)), *things]
        for item_id, definition in _shotgun(damage, spread).items():
            state.item_definitions.set_custom(item_id, definition)
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        player = logic.player_runtime.player
        player.pos.x, player.pos.y, player.pos.z = 0.0, 40.0, 0.0
        player.angle, player.pitch = 0.0, 0.0
        combat = logic.combat_runtime
        combat.give_weapon(combat.items.resolve("custom2"))
        logic.game_state.consume_sounds()
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _record_outputs(logic, monkeypatch):
    fired = []
    real = logic.io_manager.fire_output

    def record(entity, output, *args, **kwargs):
        fired.append((entity.properties.get("name"), output))
        return real(entity, output, *args, **kwargs)

    monkeypatch.setattr(logic.io_manager, "fire_output", record)
    return fired


def _shoot(logic):
    combat = logic.combat_runtime
    combat._last_player_shot_time = float("-inf")
    combat._handle_shooting()


def _health(thing):
    return thing.properties.get("health")


# -- kills are shared across the pellets of a shot ----------------------------------

def test_a_pending_hit_on_a_monster_an_earlier_pellet_killed_is_skipped(world, monkeypatch):
    front = _monster_ahead("front", 200, health=10)
    back = _monster_ahead("back", 400)
    logic = world([front, back])
    fired = _record_outputs(logic, monkeypatch)

    _shoot(logic)

    # Pellet 0 kills the front monster; its pending hits for pellets 1-4 are
    # passed over and land on the monster behind, as the dead-check did.
    assert front.properties.get("dead") is True
    assert _health(front) == 0
    assert _health(back) == 100 - (PELLETS - 1) * 10
    # Applied pellet by pellet: the death is fired before any later pellet's hit.
    assert fired == ([("front", "OnDamaged"), ("front", "OnDeath")]
                     + [("back", "OnDamaged")] * (PELLETS - 1))


def test_the_kill_is_shared_whichever_pellet_makes_it(world, monkeypatch):
    # 25 health and 10 a pellet: pellets 0 and 1 wound, pellet 2 kills, and
    # pellets 3 and 4 pass over the corpse to the monster behind.
    front = _monster_ahead("front", 200, health=25)
    back = _monster_ahead("back", 400)
    logic = world([front, back])
    fired = _record_outputs(logic, monkeypatch)

    _shoot(logic)

    assert front.properties.get("dead") is True
    assert _health(back) == 100 - 2 * 10
    assert fired == [("front", "OnDamaged"), ("front", "OnDamaged"),
                     ("front", "OnDamaged"), ("front", "OnDeath"),
                     ("back", "OnDamaged"), ("back", "OnDamaged")]


def test_a_killed_monster_with_nothing_behind_lets_later_pellets_mark_the_wall(world):
    front = _monster_ahead("front", 200, health=10)
    wall = box_brush("wall", (0, 80, 600), (512, 512, 16))
    logic = world([front], brushes=[wall])
    combat = logic.combat_runtime

    _shoot(logic)

    assert front.properties.get("dead") is True
    assert len(combat.bullet_marks) == PELLETS - 1


def test_a_monster_survives_pellets_that_do_not_kill_it_and_takes_each(world):
    target = _monster_ahead("target", 300)
    logic = world([target])
    _shoot(logic)
    assert _health(target) == 100 - PELLETS * 10
    assert not target.properties.get("dead")


def test_a_monster_dead_before_the_shot_is_not_in_the_snapshot(world):
    corpse = _monster_ahead("corpse", 200)
    back = _monster_ahead("back", 400)
    logic = world([corpse, back])
    corpse.properties["dead"] = True   # after Play, which revives monsters
    _shoot(logic)
    assert _health(corpse) == 100
    assert _health(back) == 100 - PELLETS * 10


# -- effects apply in pellet (scatter) order -----------------------------------------

#: Wide enough that each pellet of the sunflower pattern can have a monster of
#: its own, far enough out that no other pellet passes within its hit sphere.
WIDE_SPREAD = 20.0


def test_effects_apply_in_pellet_scatter_order(world, monkeypatch):
    from engine.logic_combat import LogicCombat
    import glm
    # High above the floor, so no pellet's ray can reach it first.
    lift = 1000.0
    origin = glm.vec3(0.0, EYE_Y + lift, 0.0)
    aim = glm.vec3(0.0, 0.0, 1.0)
    # Pellet p's own monster sits on its scatter direction. The nearest is the
    # last pellet's and the things list holds them last pellet first, so
    # neither distance nor things order can produce pellet order by accident.
    # At these distances every other pellet passes 120+ units from a monster
    # whose hit sphere has radius 51.2.
    monsters = []
    for pellet in range(PELLETS):
        direction = LogicCombat._scatter(aim, WIDE_SPREAD, pellet)
        centre = origin + direction * (1000.0 - 100.0 * pellet)
        monsters.append(make_thing(
            Monster, "pellet%d" % pellet,
            (centre.x, centre.y - 128 * 0.45, centre.z),
            health=100, sprite_width=64, sprite_height=128))
    logic = world(list(reversed(monsters)), spread=WIDE_SPREAD)
    logic.player_runtime.player.pos.y += lift
    fired = _record_outputs(logic, monkeypatch)

    _shoot(logic)

    assert [_health(m) for m in monsters] == [90] * PELLETS, (
        "each pellet must hit its own monster for this test to say anything")
    assert fired == [("pellet%d" % p, "OnDamaged") for p in range(PELLETS)]
    hits = [s.get("entity_id") for s in logic.game_state.consume_sounds()
            if s.get("file") == "hit.wav"]
    assert hits == [id(m) for m in monsters]


# -- I/O reaches the next shot, not the rest of this one -----------------------------

def test_io_hiding_the_target_does_not_stop_the_rest_of_the_shot(world):
    target = _monster_ahead("target", 300)
    io.add_connection(target, OutputConnection(
        output_name="OnDamaged", target_name="target", input_name="Hide"))
    logic = world([target])

    _shoot(logic)

    # Pellet 0's OnDamaged hid the target at once (a delay-0 connection runs
    # synchronously), but every pellet traced the instant before it.
    assert target.properties.get("hidden") is True
    assert _health(target) == 100 - PELLETS * 10

    _shoot(logic)                      # the next shot sees it hidden
    assert _health(target) == 100 - PELLETS * 10


def test_io_revealing_a_monster_does_not_put_it_in_front_of_this_shot(world):
    revealed = _monster_ahead("revealed", 150, hidden=True)
    target = _monster_ahead("target", 400)
    io.add_connection(target, OutputConnection(
        output_name="OnDamaged", target_name="revealed", input_name="Show"))
    logic = world([revealed, target])

    _shoot(logic)

    assert not revealed.properties.get("hidden")
    assert _health(revealed) == 100
    assert _health(target) == 100 - PELLETS * 10

    _shoot(logic)                      # now it is in the way
    assert _health(revealed) == 100 - PELLETS * 10
    assert _health(target) == 100 - PELLETS * 10


def test_io_killing_the_target_does_not_turn_away_the_rest_of_the_shot(world, monkeypatch):
    # Only a pellet's own damage kills for the other pellets; a death I/O
    # causes is, like any other I/O, for the next shot.
    target = _monster_ahead("target", 300)
    back = _monster_ahead("back", 500)
    logic = world([target, back])
    real = logic.io_manager.fire_output

    def kill_on_damage(entity, output, *args, **kwargs):
        if entity is target and output == "OnDamaged":
            target.properties["dead"] = True
        return real(entity, output, *args, **kwargs)

    monkeypatch.setattr(logic.io_manager, "fire_output", kill_on_damage)

    _shoot(logic)

    assert _health(target) == 100 - PELLETS * 10
    assert _health(back) == 100


# -- one snapshot, one lock ------------------------------------------------------------

class _CountingLock:
    def __init__(self, lock):
        self._lock = lock
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)


def test_a_shot_takes_monster_lock_once_for_all_its_pellets(world):
    target = _monster_ahead("target", 300)
    logic = world([target])
    session = logic.session_runtime
    counting = _CountingLock(session.monster_lock)
    session.monster_lock = counting
    try:
        logic.combat_runtime._fire_rays(
            logic.combat_runtime.held_weapon(), *_aim_pellets(logic))
    finally:
        session.monster_lock = counting._lock
    assert counting.entered == 1
    assert _health(target) == 100 - PELLETS * 10


def _aim_pellets(logic):
    combat = logic.combat_runtime
    weapon = combat.held_weapon()
    origin, direction = combat._aim()
    return origin, [combat._scatter(direction, weapon.spread, pellet)
                    for pellet in range(weapon.pellets)]
