"""The player's shots hit the monster that is drawn, not the air above it.

``Monster.pos`` is the centre of the billboard: the AI grounds a monster at
``floor + sprite_height / 2`` and the sprite pass draws the quad centred on
``pos``. The hitscan sphere was measured from ``pos`` as if it were the
feet (``pos.y + 0.45 * sprite_height``) and the projectile sphere sat 64
units above ``pos``, so on the default 128x192 human both spheres were on its
head: a shot at the body from eye height (the player's eye is 90 above the
floor) missed, and the monster never bled or died.

The monster here is the one from the report: type 'human', no variant,
Monster Id 0, 128 wide and 192 tall, AI range 512, team '(none)', patrol and
'Can hear gunfire' off.
"""

import math

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import Monster, PlayerStart            # noqa: E402
from engine.items import DEFAULT_DEFINITIONS              # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

WIDTH, HEIGHT = 128.0, 192.0
DISTANCE = 400.0


def _reported_monster():
    monster = make_thing(
        Monster, "soldier", (0.0, 0.0, DISTANCE),
        monster_type="human", variant="<None>", monster_id=0,
        sprite_width=int(WIDTH), sprite_height=int(HEIGHT), ai_range=512,
        team="(none)", patrol=False, hear_gunfire=False)
    # Standing on the floor (top at y=0), where the AI grounds it.
    monster.pos = [0.0, HEIGHT / 2.0, DISTANCE]
    return monster


@pytest.fixture
def world():
    made = []

    def _build(monster, items=None):
        state = EditorState()
        state.brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096))]
        state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0)), monster]
        for item_id, definition in (items or {}).items():
            state.item_definitions.set_custom(item_id, definition)
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        player = logic.player_runtime.player
        # Feet on the floor: the hull is centred half its height above them.
        player.pos.x, player.pos.y, player.pos.z = 0.0, player.height / 2.0, 0.0
        player.angle, player.pitch = 0.0, 0.0
        combat = logic.combat_runtime
        combat.set_gore_level(1)
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _aim_at(logic, monster, fraction):
    """Pitch the view at the point *fraction* of the way up the sprite."""
    player = logic.player_runtime.player
    eye_y = player.pos.y + player.camera_height
    target_y = monster.pos[1] - HEIGHT / 2.0 + HEIGHT * fraction
    player.angle = 0.0
    player.pitch = math.atan2(target_y - eye_y, monster.pos[2] - player.pos.z)


def _fire(logic):
    combat = logic.combat_runtime
    combat._last_player_shot_time = float("-inf")
    combat.player_ammo = 999
    combat._handle_shooting()


@pytest.mark.parametrize("fraction", [None, 0.2, 0.35, 0.5, 0.8])
def test_a_pistol_shot_at_the_body_hurts_bleeds_and_kills(world, fraction):
    monster = _reported_monster()
    logic = world(monster)
    combat = logic.combat_runtime
    combat.give_weapon(combat.items.resolve("gun1"))
    if fraction is not None:            # None: level, straight from the eye
        _aim_at(logic, monster, fraction)

    _fire(logic)

    assert monster.properties["health"] < 100, "the shot missed the drawn sprite"
    assert monster.properties.get("_wounds", 0) > 0
    assert combat.blood.count > 0
    for _ in range(50):
        if monster.properties.get("dead"):
            break
        _fire(logic)
    assert monster.properties.get("dead") is True


def test_the_hit_sphere_sits_on_the_drawn_sprite(world):
    monster = _reported_monster()
    logic = world(monster)
    _, centres, radii = logic.combat_runtime._hitscan_monster_snapshot()
    feet_y = monster.pos[1] - HEIGHT / 2.0
    centre_y, radius = float(centres[0][1]), float(radii[0])
    # Inside the quad, a little below its middle ...
    assert feet_y < centre_y < monster.pos[1]
    # ... reaching the feet, and not floating far over the head.
    assert centre_y - radius <= feet_y
    assert centre_y + radius < feet_y + HEIGHT * 1.2


def test_a_player_projectile_fired_level_hits_the_monster(world):
    definition = dict(DEFAULT_DEFINITIONS["custom1"])
    definition["weapon"] = {**definition.get("weapon", {}),
                            "mode": "projectile", "damage": 40.0,
                            "projectile_speed": 600.0, "range": 5000.0,
                            "ammo": 0, "ammo_per_shot": 0, "cooldown": 0.0}
    monster = _reported_monster()
    logic = world(monster, items={"custom1": definition})
    combat = logic.combat_runtime
    combat.give_weapon(combat.items.resolve("custom1"))

    _fire(logic)
    for _ in range(90):
        combat._update_monster_projectiles(1.0 / 60.0)

    assert monster.properties["health"] == 60
