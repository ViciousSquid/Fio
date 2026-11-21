"""Fast projectiles are swept, not sampled.

A projectile used to be tested only where it ended each tick: one moving
further in a tick than a wall is thick (16 units at 60 Hz is 960 u/s) could
pass straight through it, and one moving further than an actor is wide
straight through the actor. A step longer than PROJECTILE_SWEEP_STEP now has
its path tested against the walls, the player, the monsters and the barrels,
and stops at the first; both the scalar (small swarm) and the dense path.
"""

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import Monster, PlayerStart            # noqa: E402
from engine.logic_combat import LogicCombat, PLAYER_PROJECTILE_OWNER  # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0
SOMEONE = 12345            # a projectile's owner that is neither player nor monster


@pytest.fixture
def world():
    made = []

    def _build(things=(), brushes=(), player_at=(0.0, 40.0, 0.0)):
        state = EditorState()
        state.brushes = [box_brush("floor", (0, -16, 0), (8192, 32, 8192)), *brushes]
        state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0)), *things]
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        player = logic.player_runtime.player
        player.pos.x, player.pos.y, player.pos.z = player_at
        logic.player_runtime.player_health = 100
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _run(logic, ticks, dense):
    combat = logic.combat_runtime
    combat.PROJECTILE_DENSE_THRESHOLD = 0 if dense else 100
    for _ in range(ticks):
        combat._update_monster_projectiles(TICK)


@pytest.mark.parametrize("dense", [False, True])
def test_a_fast_shot_does_not_pass_through_a_thin_wall(world, dense):
    # The player at z=0; a 16-unit wall at z=300; the shot from z=600 at
    # 6000 u/s moves 100 units a tick -- its end points straddle the wall.
    wall = box_brush("wall", (0, 64, 300), (1024, 256, 16))
    logic = world(brushes=[wall])
    combat = logic.combat_runtime
    combat._add_monster_projectile((0.0, 40.0, 603.0), (0.0, 0.0, -6000.0), SOMEONE, 30, 5.0)
    _run(logic, 20, dense)
    assert logic.player_runtime.player_health == 100, "the shot went through the wall"
    assert len(combat._monster_projectiles) == 0          # the wall stopped it
    assert combat.projectile_sweeps >= 1


@pytest.mark.parametrize("dense", [False, True])
def test_a_fast_shot_does_not_pass_through_an_actor(world, dense):
    # 30000 u/s is 500 units a tick: far wider than a monster's 128-unit hit
    # sphere, so the end points alone would never land inside it.
    target = make_thing(Monster, "target", (0.0, 40.0, 1000.0), health=100)
    logic = world(things=[target], player_at=(0.0, 40.0, -3000.0))
    logic.world_runtime.build_entity_caches()
    combat = logic.combat_runtime
    combat._add_monster_projectile((0.0, 40.0, 1700.0), (0.0, 0.0, -30000.0),
                                   PLAYER_PROJECTILE_OWNER, 40, 5.0)
    _run(logic, 4, dense)
    assert int(target.properties["health"]) == 60


@pytest.mark.parametrize("dense", [False, True])
def test_a_fast_shot_hits_the_player_it_would_have_skipped(world, dense):
    logic = world()
    combat = logic.combat_runtime
    # 250 units a tick against a 64-unit sphere, landing either side of it.
    combat._add_monster_projectile((0.0, 40.0, 130.0), (0.0, 0.0, -15000.0), SOMEONE, 30, 5.0)
    _run(logic, 2, dense)
    assert logic.player_runtime.player_health == 70


@pytest.mark.parametrize("dense", [False, True])
def test_a_shot_at_the_default_speed_is_never_swept(world, dense):
    """900 u/s is 15 units a tick: the point tests already catch a 16-unit
    wall, and no sweep is paid for."""
    logic = world(player_at=(5000.0, 40.0, 5000.0))
    combat = logic.combat_runtime
    for k in range(8):
        combat._add_monster_projectile((k * 50.0, 40.0, 0.0), (0.0, 0.0, 900.0),
                                       SOMEONE, 5, 1.0)
    _run(logic, 30, dense)
    assert combat.projectile_sweeps == 0
    assert 900.0 * TICK < LogicCombat.PROJECTILE_SWEEP_STEP


def test_an_unobstructed_fast_shot_flies_on(world):
    logic = world(player_at=(5000.0, 40.0, 5000.0))
    combat = logic.combat_runtime
    combat._add_monster_projectile((0.0, 40.0, 0.0), (0.0, 0.0, 6000.0), SOMEONE, 5, 5.0)
    _run(logic, 3, dense=False)
    assert len(combat._monster_projectiles) == 1
    assert float(combat._monster_projectiles.pos[0][2]) == pytest.approx(300.0, abs=0.1)
