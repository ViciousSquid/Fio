"""Items at play time: firing, slots, pickups and armor, all from definitions.

These drive the real ``LogicThread`` over a small world. Each weapon behaviour
is read from the session's compiled item registry, so a custom slot configured
in the editor fires, sounds, spends ammunition and is collected exactly as its
definition says -- and gun1/gun2 still play as the pistol and the shotgun.
"""

import copy

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import Monster, PlayerStart, Prop      # noqa: E402
from engine import savegame                               # noqa: E402
from engine.items import DEFAULT_DEFINITIONS              # noqa: E402
from engine.logic_combat import PLAYER_PROJECTILE_OWNER   # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

#: The player's eye: Player(0, 0) at y=40 with camera_height 40, facing +z.
EYE_Y = 80.0


def _custom(item_id, **changes):
    definition = copy.deepcopy(DEFAULT_DEFINITIONS[item_id])
    for key, value in changes.items():
        if isinstance(value, dict):
            definition[key] = {**definition.get(key, {}), **value}
        else:
            definition[key] = value
    return definition


def _monster_ahead(name, distance, **props):
    """A monster whose hitscan sphere is centred on the eye ray *distance* ahead."""
    return make_thing(Monster, name, (0, EYE_Y - 128 * 0.45, distance),
                      health=100, **props)


@pytest.fixture
def world():
    made = []

    def _build(things=(), brushes=(), items=None):
        state = EditorState()
        state.brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096)), *brushes]
        state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0)), *things]
        for item_id, definition in (items or {}).items():
            state.item_definitions.set_custom(item_id, definition)
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        player = logic.player_runtime.player
        player.pos.x, player.pos.y, player.pos.z = 0.0, 40.0, 0.0
        player.angle, player.pitch = 0.0, 0.0
        logic.game_state.consume_sounds()
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _arm(logic, item_id):
    combat = logic.combat_runtime
    combat.give_weapon(combat.items.resolve(item_id))
    return combat


def _sounds(logic):
    return [s.get("file") for s in logic.game_state.consume_sounds()]


def _health(thing):
    return thing.properties.get("health")


# -- built-in weapons are unchanged ---------------------------------------------

def test_play_starts_unarmed_with_the_maps_registry(world):
    logic = world(items={"custom1": _custom("custom1", name="Cigar")})
    combat = logic.combat_runtime
    assert (combat.active_weapon, combat.weapons, combat.player_ammo) == (None, set(), 0)
    assert combat.items is logic.editor_state.item_definitions.registry()
    assert combat.items.resolve("custom1").name == "Cigar"


def test_the_pistol_hits_for_25_with_infinite_ammo_and_its_sound(world):
    target = _monster_ahead("target", 300)
    logic = world([target])
    combat = _arm(logic, "gun1")
    assert combat.player_ammo == 0

    combat._handle_shooting()

    assert _health(target) == 75
    assert isinstance(_health(target), int)     # as the hard-coded pistol left it
    assert combat.player_ammo == 0
    assert combat.muzzle_flash_active
    assert "shoot.wav" in _sounds(logic)
    # A pistol has no cooldown: the next shot goes straight off.
    combat._handle_shooting()
    assert _health(target) == 50


def test_the_shotgun_gives_eight_shells_spends_one_and_cools_down(world):
    target = _monster_ahead("target", 300)
    logic = world([target])
    combat = _arm(logic, "gun2")
    assert combat.player_ammo == 8

    combat._handle_shooting()
    combat._handle_shooting()          # still cooling down: nothing happens

    assert _health(target) == 25
    assert combat.player_ammo == 7
    assert _sounds(logic).count("shoot2.wav") == 1


def test_a_weapon_with_no_ammo_does_not_fire(world):
    target = _monster_ahead("target", 300)
    logic = world([target])
    combat = _arm(logic, "gun2")
    combat.player_ammo = 0

    combat._handle_shooting()

    assert _health(target) == 100
    assert not combat.muzzle_flash_active
    assert _sounds(logic) == []


def test_a_second_shotgun_pickup_adds_no_ammo(world):
    logic = world()
    combat = _arm(logic, "gun2")
    combat.player_ammo = 3
    _arm(logic, "gun2")
    assert combat.player_ammo == 3


@pytest.mark.parametrize("item_id", ["custom1", "custom2"])
def test_default_custom_items_are_held_but_never_fire(world, item_id):
    target = _monster_ahead("target", 300)
    logic = world([target])
    combat = _arm(logic, item_id)

    combat._handle_shooting()

    assert combat.active_weapon == item_id
    assert _health(target) == 100
    assert not combat.muzzle_flash_active
    assert _sounds(logic) == []
    assert combat.get_recent_noise_events() == []


# -- custom weapons fire as configured ----------------------------------------------

def test_a_custom_hitscan_weapon_uses_its_own_damage_ammo_sound_and_cooldown(world):
    target = _monster_ahead("target", 300)
    logic = world([target], items={"custom1": _custom("custom1", weapon={
        "mode": "hitscan", "damage": 30.0, "ammo": 3, "ammo_per_shot": 1,
        "cooldown": 60.0, "sound": "shoot2.wav", "noise": 0.5})})
    combat = _arm(logic, "custom1")
    assert combat.player_ammo == 3

    combat._handle_shooting()
    combat._handle_shooting()          # 60 s cooldown

    assert _health(target) == 70
    assert combat.player_ammo == 2
    assert _sounds(logic) == ["shoot2.wav", "hit.wav"]
    assert combat.shot_ready(float("inf"))


@pytest.mark.parametrize("reach, health", [(100.0, 100), (1000.0, 90)])
def test_a_custom_weapons_range_limits_its_reach(world, reach, health):
    target = _monster_ahead("target", 300)
    logic = world([target], items={"custom1": _custom("custom1", weapon={
        "mode": "hitscan", "damage": 10.0, "range": reach})})
    _arm(logic, "custom1")._handle_shooting()
    assert _health(target) == health


def test_pellets_each_deal_damage(world):
    target = _monster_ahead("target", 200)
    logic = world([target], items={"custom2": _custom("custom2", weapon={
        "mode": "hitscan", "damage": 10.0, "pellets": 5, "spread": 1.0})})
    combat = _arm(logic, "custom2")
    combat._handle_shooting()
    assert _health(target) == 50


_MELEE = {"custom1": _custom("custom1", weapon={
    "mode": "melee", "damage": 50.0, "range": 64.0})}


@pytest.mark.parametrize("distance, health", [(100, 50), (200, 100)])
def test_melee_reaches_only_its_range(world, distance, health):
    target = _monster_ahead("target", distance)
    logic = world([target], items=_MELEE)
    _arm(logic, "custom1")._handle_shooting()
    assert _health(target) == health


def test_melee_leaves_no_mark_on_the_wall_it_strikes(world):
    wall = box_brush("wall", (0, 80, 60), (256, 256, 16))
    logic = world(brushes=[wall], items=_MELEE)
    combat = _arm(logic, "custom1")
    combat._handle_shooting()
    assert combat.muzzle_flash_active
    assert combat.bullet_marks == []


def test_hitscan_marks_the_wall_it_misses_onto(world):
    wall = box_brush("wall", (0, 80, 60), (256, 256, 16))
    logic = world(brushes=[wall])
    combat = _arm(logic, "gun1")
    combat._handle_shooting()
    assert len(combat.bullet_marks) == 1


def test_a_projectile_weapon_fires_into_the_shared_projectile_store(world):
    target = make_thing(Monster, "target", (0, EYE_Y - 64, 300), health=100)
    logic = world([target], items={"custom1": _custom("custom1", weapon={
        "mode": "projectile", "damage": 40.0, "projectile_speed": 600.0})})
    combat = _arm(logic, "custom1")

    combat._handle_shooting()

    store = combat._monster_projectiles
    assert len(store) == 1
    assert int(store.owner_id[0]) == PLAYER_PROJECTILE_OWNER
    assert float(store.damage[0]) == 40.0
    assert _health(target) == 100          # not hitscan: nothing yet
    for _ in range(60):
        combat._update_monster_projectiles(1.0 / 60.0)
    assert _health(target) == 60
    assert len(store) == 0


def test_the_players_projectile_never_hurts_the_player(world):
    logic = world()
    combat = logic.combat_runtime
    player = logic.player_runtime.player
    at_player = (float(player.pos.x), float(player.pos.y), float(player.pos.z))

    combat._add_monster_projectile(at_player, (0, 0, 0), PLAYER_PROJECTILE_OWNER, 30, 5.0)
    combat._update_monster_projectiles(0.0)
    assert logic.player_runtime.player_health == 100

    # Control: a monster's projectile in the same place does.
    combat._add_monster_projectile(at_player, (0, 0, 0), 12345, 30, 5.0)
    combat._update_monster_projectiles(0.0)
    assert logic.player_runtime.player_health == 70


# -- slots 1-4 ----------------------------------------------------------------------

def test_slot_keys_switch_only_to_weapons_the_player_has(world):
    logic = world()
    combat = _arm(logic, "gun1")
    _arm(logic, "custom2")
    serial = combat.weapon_switch_serial

    combat.select_slot(2)               # no shotgun
    assert combat.active_weapon == "custom2"
    assert combat.weapon_switch_serial == serial

    combat.select_slot(1)
    assert combat.active_weapon == "gun1"
    assert combat.weapon_switch_serial == serial + 1

    combat.select_slot(4)
    assert combat.active_weapon == "custom2"
    combat.select_slot(0)
    combat.select_slot(5)
    assert combat.active_weapon == "custom2"
    assert combat.weapon_switch_serial == serial + 2


def test_slot_keys_go_through_the_play_tick(world):
    logic = world()
    _arm(logic, "gun1")
    _arm(logic, "gun2")
    logic.game_state.queue_weapon_slot(3)       # not held: ignored
    logic.game_state.queue_weapon_slot(1)
    logic._tick_play_mode(1.0 / 60.0)
    assert logic.combat_runtime.active_weapon == "gun1"
    assert logic.game_state.consume_weapon_slots() == []


# -- pickups from definitions ------------------------------------------------

def _item_prop(name, item_id, pos=(0, 40, 0), **props):
    return make_thing(Prop, name, pos, collect_enabled=True, collect_type="item",
                      collect_item=item_id, **props)


def test_a_custom_armor_pickup_gives_armor_and_names_itself(world):
    pickup = _item_prop("vest", "custom1")
    logic = world([pickup], items={"custom1": _custom(
        "custom1", name="Vest", kind="pickup", pickup={"effect": "armor", "amount": 40})})

    assert logic.prop_runtime.collect_prop(pickup) is True

    assert logic.player_runtime.player_armor == 40
    assert logic.combat_runtime.weapons == set()
    assert logic.interaction_runtime.current_hud_message == "Collected Vest"


@pytest.mark.parametrize("effect, amount, check", [
    ("health", 15, lambda logic: logic.player_runtime.player_health == 100),
    ("ammo", 6, lambda logic: logic.combat_runtime.player_ammo == 6),
    ("key", 0, lambda logic: "red_key" in logic.player_runtime.collected_keys),
    ("weapon", 0, lambda logic: logic.combat_runtime.active_weapon == "gun2"),
])
def test_custom_pickup_effects(world, effect, amount, check):
    pickup = _item_prop("pickup", "custom2")
    logic = world([pickup], items={"custom2": _custom("custom2", kind="pickup", pickup={
        "effect": effect, "amount": amount, "key_name": "red_key", "item_id": "gun2"})})
    assert logic.prop_runtime.collect_prop(pickup) is True
    assert check(logic)


def test_a_health_pickup_heals(world):
    pickup = _item_prop("flask", "custom2")
    logic = world([pickup], items={"custom2": _custom("custom2", kind="pickup", pickup={
        "effect": "health", "amount": 15})})
    logic.player_runtime.player_health = 50
    logic.prop_runtime.collect_prop(pickup)
    assert logic.player_runtime.player_health == 65


def test_a_custom_weapon_pickup_is_held_and_selectable_by_slot(world):
    pickup = _item_prop("glass", "custom2")
    logic = world([pickup])
    assert logic.prop_runtime.collect_prop(pickup) is True
    combat = logic.combat_runtime
    assert combat.active_weapon == "custom2"
    assert combat.weapons == {"custom2"}
    assert logic.interaction_runtime.current_hud_message == "Collected Wine Glass"


def test_an_invalid_item_is_never_collected_and_never_becomes_the_pistol(world):
    pickup = _item_prop("broken", "custom1")
    logic = world([pickup])
    logic.editor_state.item_definitions.load(
        {"custom1": {"name": "Broken", "weapon": {"mode": "laser"}}})
    logic.combat_runtime.items = logic.editor_state.item_definitions.registry()

    assert logic.prop_runtime.collect_prop(pickup) is False

    assert logic.combat_runtime.active_weapon is None
    assert logic.combat_runtime.weapons == set()
    assert not pickup.properties.get("collect_collected")


def test_an_unknown_item_id_is_never_collected(world):
    pickup = _item_prop("mystery", "gun9")
    logic = world([pickup])
    assert logic.prop_runtime.collect_prop(pickup) is False
    assert logic.combat_runtime.active_weapon is None


def test_a_use_activated_pickup_is_not_collected_by_walking_over_it(world):
    pickup = _item_prop("vest", "custom1")
    logic = world([pickup], items={"custom1": _custom("custom1", kind="pickup", pickup={
        "effect": "armor", "amount": 10, "activation": "use"})})
    logic.prop_runtime._collect_walk_over()
    assert logic.player_runtime.player_armor == 0
    assert logic.prop_runtime._activation(pickup) == "use"


def test_a_use_activated_pickup_is_collected_by_using_it(world):
    # In front of the eye, within use reach.
    pickup = _item_prop("vest", "custom1", pos=(0, EYE_Y, 40))
    logic = world([pickup], items={"custom1": _custom("custom1", kind="pickup", pickup={
        "effect": "armor", "amount": 10, "activation": "use"})})
    logic.prop_runtime.tick(1.0 / 60.0, False)
    assert logic.player_runtime.player_armor == 0
    logic.prop_runtime.tick(1.0 / 60.0, True)
    assert logic.player_runtime.player_armor == 10
    assert pickup.properties["collect_collected"] is True


def test_a_respawning_pickup_comes_back_as_its_definition_says(world):
    pickup = _item_prop("vest", "custom1")
    logic = world([pickup], items={"custom1": _custom("custom1", kind="pickup", pickup={
        "effect": "armor", "amount": 10, "respawns": True, "respawn_time": 2.0})})
    assert logic.prop_runtime.collect_prop(pickup) is True
    timer = logic.prop_runtime.respawn_timers[id(pickup)]
    assert timer["remaining"] == 2.0


# -- armor is applied in the one player-damage path -----------------------------

def test_armor_absorbs_damage_before_health(world):
    logic = world()
    logic.player_runtime.give_armor(30)
    logic.trigger_runtime._apply_player_damage(20)
    assert (logic.player_runtime.player_armor, logic.player_runtime.player_health) == (10, 100)
    logic.trigger_runtime._apply_player_damage(25)
    assert (logic.player_runtime.player_armor, logic.player_runtime.player_health) == (0, 85)


def test_armor_is_capped(world):
    logic = world()
    logic.player_runtime.give_armor(80)
    logic.player_runtime.give_armor(80)
    assert logic.player_runtime.player_armor == logic.player_runtime.player_max_armor == 100


def test_monster_projectiles_respect_armor(world):
    logic = world()
    logic.player_runtime.give_armor(20)
    player = logic.player_runtime.player
    logic.combat_runtime._add_monster_projectile(
        (float(player.pos.x), float(player.pos.y), float(player.pos.z)),
        (0, 0, 0), 999, 30, 5.0)
    logic.combat_runtime._update_monster_projectiles(0.0)
    assert (logic.player_runtime.player_armor, logic.player_runtime.player_health) == (0, 90)


def test_play_starts_without_armor(world):
    logic = world()
    logic.player_runtime.give_armor(50)
    logic.session_runtime.apply_play_mode(False)
    logic.session_runtime.apply_play_mode(True)
    assert logic.player_runtime.player_armor == 0


# -- publication and saves ------------------------------------------------------

def test_the_render_frame_publishes_armor_switches_and_readiness(world):
    logic = world()
    combat = _arm(logic, "gun2")
    _arm(logic, "gun1")
    logic.player_runtime.give_armor(5)
    combat.select_slot(2)

    logic.render_runtime.prepare_render_state()
    assert logic.game_state.request_swap()

    published = logic.game_state.published
    assert published("active_weapon") == "gun2"
    assert published("weapon_switch_serial") == combat.weapon_switch_serial == 1
    assert published("player_armor") == 5
    assert published("shot_ready") is True

    combat.player_ammo = 0
    logic.render_runtime.prepare_render_state()
    assert logic.game_state.request_swap()
    assert logic.game_state.published("shot_ready") is False


def test_a_save_carries_every_weapon_and_the_armor(world):
    logic = world()
    _arm(logic, "gun2")
    _arm(logic, "custom1")
    logic.combat_runtime.player_ammo = 4
    logic.player_runtime.give_armor(25)
    data = savegame.build_snapshot(logic, map_name="items.json")
    assert data["save_version"] == 3
    assert data["runtime"]["weapons"] == ["custom1", "gun2"]

    logic.combat_runtime.reset_weapons()
    logic.player_runtime.player_armor = 0
    savegame.restore_snapshot(logic, data)

    combat = logic.combat_runtime
    assert (combat.active_weapon, combat.weapons, combat.player_ammo) == (
        "custom1", {"custom1", "gun2"}, 4)
    assert logic.player_runtime.player_armor == 25


def test_a_version_2_save_restores_the_weapons_it_recorded(world):
    logic = world()
    data = savegame.build_snapshot(logic, map_name="items.json")
    runtime = data["runtime"]
    del runtime["weapons"]
    runtime.update(active_weapon="gun1", gun2_obtained=True, player_ammo=5)
    data["save_version"] = 2

    savegame.restore_snapshot(logic, data)

    combat = logic.combat_runtime
    assert (combat.active_weapon, combat.weapons, combat.player_ammo) == (
        "gun1", {"gun1", "gun2"}, 5)
