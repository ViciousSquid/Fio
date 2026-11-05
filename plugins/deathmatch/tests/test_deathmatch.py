"""Integration coverage using native Fio logic, weapons, AI and physics."""
import copy
import json
import math
from pathlib import Path

import glm
import pytest

from editor.editor_state import EditorState
from editor.things import Thing
from engine.logic_thread import LogicThread
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState
from plugins.manager import get_manager, load_plugins
from plugins.deathmatch.entities import (
    DeathmatchSettings, DeathmatchBot, DeathmatchSpawn, DeathmatchNode,
)
from plugins.deathmatch.navigation import Navigation

pytestmark = pytest.mark.qt


def brush(pos, size):
    return dict(pos=pos, size=size, is_trigger=False)


@pytest.fixture
def arena():
    load_plugins()
    manager = get_manager()
    plugin = manager.find_plugin("deathmatch")
    was_enabled = plugin.enabled
    manager.set_enabled(plugin, True)
    state = EditorState()
    state.brushes = [brush([0, -25, 0], [4000, 50, 4000])]
    state.things = [DeathmatchSettings(properties={"frag_limit": 0, "spawn_protection": 0}),
                    DeathmatchBot([0, 0, 300], {"name": "Bot"}),
                    DeathmatchSpawn([0, 0, 0]), DeathmatchSpawn([0, 0, 600])]
    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player = Player(0, 0)
    logic.session_runtime.apply_play_mode(True)
    session = plugin._sessions[logic]
    human = session.fighters["player"]
    bot = next(f for f in session.fighters.values() if f.entity is not None)
    place(human, [0, 0, 0])
    place(bot, [0, 0, 300])
    yield logic, session, human, bot
    logic.session_runtime.apply_play_mode(False)
    logic.stop()
    manager.set_enabled(plugin, was_enabled)


def place(fighter, feet, angle=0):
    fighter.actor.pos = glm.vec3(feet) + glm.vec3(0, fighter.actor.height / 2, 0)
    fighter.actor.angle, fighter.actor.pitch = angle, 0
    fighter.actor.velocity = glm.vec3(0)
    fighter.protected_until = 0
    if fighter.entity is not None:
        fighter.entity.pos = feet[:]


def walls(logic, *extra):
    logic.editor_state.brushes = [logic.editor_state.brushes[0], *extra]
    logic.collision_runtime.refresh_collision_brushes_cache()
    logic.session_runtime.spatial_grid.populate(logic.editor_state.brushes)


def test_registration_and_demo_round_trip(arena):
    manager = get_manager()
    assert manager.plugin_for_type("deathmatchbot").name == "deathmatch"
    data = json.loads(Path(__file__).parents[1].joinpath("Deathmatch_Arena.json").read_text())
    entities = [Thing.from_dict(record) for record in data["things"]]
    assert sum(isinstance(t, DeathmatchBot) for t in entities) == 4
    assert sum(isinstance(t, DeathmatchSettings) for t in entities) == 1
    assert all(t is not None for t in entities)
    assert manager.required_plugins_for_map(data)[0].name == "deathmatch"


def test_native_hitscan_kill_counts_once_and_respawns(arena):
    logic, session, human, bot = arena
    for _ in range(4):
        logic.combat_runtime._handle_shooting()
    assert human.frags == 1 and bot.deaths == 1 and not bot.alive
    assert bot.entity.properties["hidden"]
    logic.combat_runtime._handle_shooting()
    assert human.frags == 1
    for _ in range(31):
        session.step(0.1)
    assert bot.alive and bot.entity.properties["health"] > 0
    assert bot.entity.properties["disabled"]  # Stock AI never takes over.


def test_native_shot_is_blocked_by_wall(arena):
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    logic.combat_runtime._handle_shooting()
    assert bot.entity.properties["health"] == 100
    assert human.frags == 0


def test_bot_shot_uses_occlusion_and_nearest_fighter(arena):
    logic, session, human, bot = arena
    place(bot, [0, 0, 300], math.pi)
    session.fire_bot(bot)
    assert logic.player_runtime.player_health == 75
    logic.player_runtime.player_health = 100
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    session.fire_bot(bot)
    assert logic.player_runtime.player_health == 100


def test_bot_can_kill_player_and_native_death_ticks_respawn(arena):
    logic, session, human, bot = arena
    logic.player_runtime.player_health = 10
    place(bot, [0, 0, 300], math.pi)
    session.fire_bot(bot)
    assert logic.player_runtime.player_dead and not human.alive
    assert bot.frags == 1 and human.deaths == 1
    bot.fire_at = 1000
    for _ in range(32):
        logic._tick_play_mode(0.1)
    assert human.alive and not logic.player_runtime.player_dead
    assert logic.player_runtime.player_health > 0


def test_spawn_protection_blocks_damage_but_firing_ends_it(arena):
    logic, session, human, bot = arena
    human.protected_until = bot.protected_until = 10
    logic.trigger_runtime._apply_player_damage(1000)
    assert logic.player_runtime.player_health == 100
    logic.combat_runtime._handle_shooting()
    assert human.protected_until == 0
    assert bot.entity.properties["health"] == 100
    session.fire_bot(bot)
    assert bot.protected_until == 0


def test_bot_vs_bot_and_world_deaths_have_correct_credit(arena):
    logic, session, human, bot = arena
    # Native Monster damage carries the attacking bot for infighting.
    logic.monster_ai._apply_monster_damage(bot.entity, 1000, attacker=bot.entity)
    assert bot.frags == -1 and human.frags == 0 and bot.deaths == 1
    session.respawn(bot)
    logic.monster_ai._apply_monster_damage(bot.entity, 1000, attacker=None)
    assert bot.frags == -2 and human.frags == 0 and bot.deaths == 2


def test_bot_hits_nearest_opponent_and_gets_frag_credit(arena):
    from plugins.deathmatch.runtime import Fighter
    logic, session, human, bot = arena
    entity = DeathmatchBot([0, 0, 150], {"name": "Other"})
    actor = Player(0, 150)
    other = Fighter(entity.properties["id"], "Other", entity, actor)
    session.fighters[other.key] = other
    place(other, [0, 0, 150])
    place(bot, [0, 0, 300], math.pi)
    for _ in range(4):
        session.fire_bot(bot)
    assert not other.alive and other.deaths == 1
    assert bot.frags == 1 and human.frags == 0
    assert logic.player_runtime.player_health == 100


def test_frag_limit_latches_and_restart_clears_round(arena):
    logic, session, human, bot = arena
    session.limit = 1
    for _ in range(4):
        logic.combat_runtime._handle_shooting()
    assert session.finished and session.snapshot["winner"] == "You"
    logic.trigger_runtime._apply_player_damage(1000)
    assert human.alive
    session.restart_requested = True
    session.step(0)
    assert not session.finished
    assert all(f.frags == f.deaths == 0 and f.alive for f in session.fighters.values())


def test_time_limit_reports_tie_and_freezes_scores(arena):
    logic, session, human, bot = arena
    session.duration = 0.2
    session.step(0.1)
    session.step(0.1)
    assert session.finished and session.snapshot["winner"] == "Draw"
    session.kill(bot, human)
    assert human.frags == 0 and bot.alive


def test_pause_does_not_advance_match_or_respawn(arena):
    logic, session, human, bot = arena
    session.kill(human, bot)
    clock = session.clock
    logic.session_runtime.set_world_paused("test", True)
    for _ in range(10):
        logic._tick_play_mode(0.1)
    assert session.clock == clock and not human.alive
    logic.session_runtime.set_world_paused("test", False)


def test_perception_does_not_see_through_walls_or_outside_fov(arena):
    logic, session, human, bot = arena
    place(bot, [0, 0, 300], math.pi)
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    session.perceive(bot)
    assert bot.target is None
    walls(logic)
    bot.actor.angle = 0
    session.perceive(bot)
    assert bot.target is None
    bot.actor.angle = math.pi
    session.perceive(bot)
    assert bot.target == "player" and bot.visible
    assert bot.fire_at >= session.clock + session.skill(bot)[0]


def test_hearing_investigates_position_without_granting_visibility(arena):
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    session.noises.append((session.clock, session.feet(human), human.key))
    session.perceive(bot)
    assert bot.last_seen == session.feet(human)
    assert bot.target is None and not bot.visible


def test_waypoints_route_around_wall_and_reject_closed_path(arena):
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    nodes = [DeathmatchNode([240, 0, 0], {"name": "a", "links": "b"}),
             DeathmatchNode([240, 0, 300], {"name": "b"})]
    nav = Navigation(nodes)
    path = nav.route([0, 0, 0], [0, 0, 300], session.clear)
    assert path and path[-1] == (0, 0, 300)
    previous = [0, 0, 0]
    for point in path:
        assert session.clear(previous, point)
        previous = point
    walls(logic, brush([0, 120, 150], [1000, 240, 50]))
    assert nav.route([0, 0, 0], [0, 0, 300], session.clear) == []


@pytest.mark.parametrize('marker_y', [-500, 90, 10000])
def test_waypoint_height_does_not_change_ground_route(arena, marker_y):
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    nodes = [DeathmatchNode([240, marker_y, 0], {'name': 'a', 'links': 'b', 'radius': 72}),
             DeathmatchNode([240, marker_y, 300], {'name': 'b'})]
    nav = Navigation(nodes, project=session.navigation_point)
    route = nav.route((0, 0, 0), (0, 0, 300), session.clear)
    assert route and route[-1] == (0, 0, 300)
    assert route[0].radius == 72
    assert all(point[1] == 0 for point in route)
    previous = (0, 0, 0)
    for point in route:
        assert session.clear(previous, point)
        previous = point


def test_waypoint_radius_advances_without_cutting_a_corner(arena):
    from plugins.deathmatch.navigation import Waypoint
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [300, 240, 50]))
    first = Waypoint((240, 0, 0), 180)
    route = [first, Waypoint((240, 0, 300)), (0, 0, 300)]
    session.nav.advance(route, (100, 0, 0), session.clear)
    assert route[0] is first  # Inside the radius, but the next diagonal hits the wall.
    session.nav.advance(route, (220, 0, 0), session.clear)
    assert route[0] == (240, 0, 300)
    session.nav.advance(route, (240, 300, 300), session.clear)
    assert route[0] == (240, 0, 300)  # Being on another storey is not arrival.


def test_direct_roaming_goal_keeps_its_authored_radius(arena):
    logic, session, human, bot = arena
    nav = Navigation([DeathmatchNode((200, 90, 300), {'name': 'roam', 'radius': 90})],
                     project=session.navigation_point)
    point = nav.point('roam', session.feet(bot))
    route = nav.route(session.feet(bot), point, session.clear)
    assert route[0].radius == 90
    nav.advance(route, (120, 0, 300), session.clear)
    assert route == []


def test_node_projection_uses_the_bot_storey_not_the_marker_height(arena):
    logic, session, human, bot = arena
    walls(logic, brush([0, 275, 0], [1000, 50, 1000]))
    marker = (0, 90, 0)
    assert session.navigation_point(marker, (0, 0, 0)) == (0, 0, 0)
    assert session.navigation_point(marker, (0, 300, 0)) == (0, 300, 0)


def test_botdebug_reads_actual_match_decisions_and_does_not_block_a_tick(arena):
    from types import SimpleNamespace
    from editor.bot_debug import collect_bots
    import threading
    logic, session, human, bot = arena
    owner = SimpleNamespace(state=logic.editor_state,
        view_3d=SimpleNamespace(logic_thread=logic, play_mode=True))
    bot.target, bot.visible, bot.mode = 'player', True, 'fight'
    row, = collect_bots(owner)['rows']
    assert row['mode'] == 'fight' and row['target'] == human.name
    assert dict(row['details'])['Target visible'] == 'yes'
    bot.mode, bot.route = 'resupply', [(100, 0, 300)]
    row, = collect_bots(owner)['rows']
    assert row['mode'] == 'resupply'
    assert row['route'] == ((100, 0, 300),)
    held, release = threading.Event(), threading.Event()
    def tick():
        with logic._tick_lock:
            held.set()
            release.wait(3)
    thread = threading.Thread(target=tick)
    thread.start()
    try:
        assert held.wait(3)
        assert collect_bots(owner) is None
    finally:
        release.set()
        thread.join(3)


def test_native_physics_keeps_bot_out_of_walls_and_on_floor(arena):
    logic, session, human, bot = arena
    walls(logic, brush([0, 120, 150], [1000, 240, 50]))
    place(bot, [0, 0, 300], math.pi)
    bot.last_seen = (0, 0, 0)
    bot.memory_until = 10
    for _ in range(120):
        session.step(1 / 60)
    assert bot.actor.pos.z >= 200
    assert bot.actor.pos.y == pytest.approx(bot.actor.height / 2, abs=1)


def test_disable_restores_authored_entities_and_methods(arena):
    logic, session, human, bot = arena
    authored = copy.deepcopy(session._authored[0][2])
    original_tick = session._hooks[0][2]
    session.stop()
    assert bot.entity.properties == authored
    assert logic._tick_play_mode == original_tick
    session.stop()  # Idempotent cleanup.


def test_ordinary_maps_are_inert(arena):
    logic, session, human, bot = arena
    plugin = get_manager().find_plugin("deathmatch")
    plugin.on_play_stop(logic)
    logic.editor_state.things = []
    plugin.on_play_start(logic)
    assert logic not in plugin._sessions


def command(logic, name, args="", play_mode=True):
    handled, reply = get_manager().dispatch_console_command(name, args, logic=logic, play_mode=play_mode)
    assert handled
    return reply


def test_console_add_remove_updates_native_indexes_and_cleans_up(arena):
    logic, session, human, bot = arena
    original_scene = list(logic.editor_state.things)
    assert command(logic, "addbot", '"New Challenger" hard') == "Added New Challenger (hard)."
    added = next(f for f in session.fighters.values() if f.name == "New Challenger")
    assert added.entity in logic.world_runtime.monster_things
    assert added.entity.properties["skill"] == "hard"
    assert added.entity.properties["disabled"] and added.alive
    assert command(logic, "addbot") == "Added Bot 1 (normal)."
    assert command(logic, "removebot") == "Removed Bot 1."
    assert command(logic, "removebot", '"new challenger"') == "Removed New Challenger."
    assert added.entity not in logic.world_runtime.monster_things
    assert command(logic, "removebot", "Bot") == "Removed Bot."
    assert command(logic, "removebot") == "No bots to remove."
    session.stop()
    assert logic.editor_state.things == original_scene
    assert bot.entity in logic.world_runtime.monster_things


def test_console_fraglimit_unlimited_query_validation_and_lowering(arena):
    logic, session, human, bot = arena
    assert command(logic, "fraglimit", "0") == "Frag limit: unlimited."
    human.frags = 30
    session.step(0)
    assert not session.finished
    assert command(logic, "fraglimit") == "Frag limit: unlimited."
    for invalid in ("-1", "1.5", "nan", "20 extra", "999999999999999999999", "１２"):
        command(logic, "fraglimit", invalid)
        assert session.limit == 0
    assert command(logic, "fraglimit", "25") == "Frag limit: 25."
    assert session.finished and session.snapshot["winner"] == "You"
    assert command(logic, "removebot") == "Removed Bot."
    assert session.snapshot["winner"] == "You"  # Final result stays latched.
    session.restart()
    assert session.limit == 25


def test_console_rejects_invalid_names_args_and_nonplay(arena):
    logic, session, human, bot = arena
    before = len(session.fighters)
    assert "first" in command(logic, "addbot", play_mode=False)
    assert "Usage" in command(logic, "addbot", '"unterminated')
    assert "Usage" in command(logic, "addbot", "Name impossible")
    assert "already exists" in command(logic, "addbot", "bot")
    assert "1–48" in command(logic, "addbot", '""')
    assert "Usage" in command(logic, "removebot", "one two")
    assert len(session.fighters) == before
    logic.session_runtime.set_world_paused("test", True)
    assert command(logic, "addbot", "easy") == "Added Bot 1 (easy)."
    assert command(logic, "removebot", session._spawned[-1].properties["id"]) == "Removed Bot 1."
    logic.session_runtime.set_world_paused("test", False)


def test_console_commands_only_exist_while_deathmatch_is_enabled(arena):
    logic, session, human, bot = arena
    manager = get_manager()
    plugin = manager.find_plugin("deathmatch")
    names = ("addbot", "removebot", "fraglimit", "dflaglight")
    assert all(manager.has_console_command(name) for name in names)
    manager.set_enabled(plugin, False)
    scene = list(logic.editor_state.things)
    for name in names:
        assert not manager.has_console_command(name)
        assert name not in manager.console_commands()
        assert manager.dispatch_console_command(name, "0", logic=logic, play_mode=True) == (False, None)
    assert logic.editor_state.things == scene
    assert logic not in plugin._sessions
    manager.set_enabled(plugin, True)
    assert all(manager.has_console_command(name) for name in names)
    assert logic in plugin._sessions


def pickup(logic, position, **properties):
    from engine.prop_entity import Prop
    entity = Prop(position, dict(collect_enabled=True, **properties))
    logic.editor_state.things.append(entity)
    logic.world_runtime.build_entity_caches()
    return entity


def test_bot_health_prop_grants_only_bot_and_shares_respawn(arena):
    logic, session, human, bot = arena
    bot.entity.properties["health"] = 30
    prop = pickup(logic, [0, 50, 300], collect_type="health", collect_value=50,
                  collect_respawns=True, collect_respawn_time=0.2)
    human_health = logic.player_runtime.player_health
    bot.supplies.collect_nearby()
    assert bot.entity.properties["health"] == 80
    assert logic.player_runtime.player_health == human_health
    assert prop.properties["collect_collected"]
    assert not logic.prop_runtime.collect_prop(prop)  # Human cannot claim it too.
    logic.prop_runtime._update_respawns(0.21)
    assert not prop.properties["collect_collected"]
    bot.supplies.collect_nearby()
    assert bot.entity.properties["health"] == 100


def test_full_health_disabled_and_occluded_props_are_not_consumed(arena):
    logic, session, human, bot = arena
    prop = pickup(logic, [0, 50, 300], collect_type="health")
    bot.supplies.collect_nearby()
    assert not prop.properties["collect_collected"]
    bot.entity.properties["health"] = 20
    prop.properties["disabled"] = True
    bot.supplies.collect_nearby()
    assert not prop.properties["collect_collected"]
    prop.properties["disabled"] = False
    prop.pos = [0, 50, 320]
    walls(logic, brush([0, 60, 310], [300, 120, 4]))
    bot.supplies.collect_nearby()
    assert not prop.properties["collect_collected"]


def test_bot_seeks_health_and_collects_use_props(arena):
    logic, session, human, bot = arena
    place(human, [-1500, 0, -1500])
    bot.entity.properties["health"] = 20
    prop = pickup(logic, [0, 50, 500], collect_type="health", collect_value=50)
    for _ in range(120):
        session.step(1 / 60)
        if prop.properties["collect_collected"]:
            break
    assert prop.properties["collect_collected"]
    assert bot.entity.properties["health"] == 70
    place(bot, [0, 0, 300])
    use_prop = pickup(logic, [0, 80, 330], collect_type="health", collect_activation="use")
    bot.supplies.collect_nearby()
    assert use_prop.properties["collect_collected"]


def test_bot_uses_collected_weapon_ammo_then_falls_back(arena):
    logic, session, human, bot = arena
    human_loadout = (set(logic.combat_runtime.weapons), logic.combat_runtime.player_ammo)
    gun = pickup(logic, [0, 50, 300], collect_type="item", collect_item="gun2")
    bot.supplies.collect_nearby()
    assert gun.properties["collect_collected"] and bot.supplies.ammo == 8
    assert bot.supplies.weapon().damage == 75
    place(bot, [0, 0, 300], math.pi)
    session.fire_bot(bot)
    assert logic.player_runtime.player_health == 25
    assert bot.supplies.ammo == 7
    assert session.clock + 1 == bot.fire_at
    assert (logic.combat_runtime.weapons, logic.combat_runtime.player_ammo) == human_loadout
    bot.supplies.ammo = 0
    assert bot.supplies.weapon().damage == 25
    ammo = pickup(logic, [0, 50, 300], collect_type="ammo", collect_value=8)
    bot.supplies.collect_nearby()
    assert ammo.properties["collect_collected"] and bot.supplies.ammo == 8
    session.respawn(bot)
    assert bot.supplies.weapons == {"gun1"} and bot.supplies.ammo == 0


def test_bot_custom_armor_pickup_absorbs_native_player_damage(arena):
    from engine.items import default_definition
    logic, session, human, bot = arena
    definition = default_definition("custom1")
    definition.update(kind="pickup", pickup=dict(activation="walk_over", effect="armor", amount=40,
                                                respawns=True, respawn_time=1, item_id="", key_name=""))
    logic.editor_state.item_definitions.set_custom("custom1", definition)
    logic.combat_runtime.items = logic.editor_state.item_definitions.registry()
    prop = pickup(logic, [0, 50, 300], collect_type="item", collect_item="custom1")
    bot.supplies.collect_nearby()
    assert bot.supplies.armor == 40
    assert id(prop) in logic.prop_runtime.respawn_timers
    logic.combat_runtime._handle_shooting()
    assert bot.entity.properties["health"] == 100 and bot.supplies.armor == 15


def test_collected_projectile_weapon_keeps_bot_kill_credit(arena):
    from engine.items import default_definition
    logic, session, human, bot = arena
    definition = default_definition("custom1")
    definition["weapon"].update(mode="projectile", damage=120, projectile_speed=900,
                                 cooldown=1, ammo=5, ammo_per_shot=1)
    logic.editor_state.item_definitions.set_custom("custom1", definition)
    logic.combat_runtime.items = logic.editor_state.item_definitions.registry()
    pickup(logic, [0, 50, 300], collect_type="item", collect_item="custom1")
    bot.supplies.collect_nearby()
    place(bot, [0, 0, 300], math.pi)
    bot.actor.pitch = -math.atan2(40, 300)
    session.fire_bot(bot)
    assert len(logic.combat_runtime._monster_projectiles) == 1
    for _ in range(30):
        logic.combat_runtime._update_monster_projectiles(1 / 60)
    assert human.deaths == 1 and bot.frags == 1
    assert logic.combat_runtime.projectile_damage_owner is None


def test_round_restart_replenishes_consumed_pickups(arena):
    logic, session, human, bot = arena
    bot.entity.properties["health"] = 20
    prop = pickup(logic, [0, 50, 300], collect_type="health")
    bot.supplies.collect_nearby()
    assert prop.properties["collect_collected"]
    session.restart()
    assert not prop.properties["collect_collected"]


@pytest.fixture
def ctf(arena):
    from plugins.deathmatch.entities import DeathmatchFlag
    logic, _, _, _ = arena
    plugin = get_manager().find_plugin("deathmatch")
    plugin.on_play_stop(logic)
    settings = next(t for t in logic.editor_state.things if isinstance(t, DeathmatchSettings))
    settings.properties.update(game_type="ctf", capture_limit=2)
    logic.editor_state.things.extend([
        DeathmatchFlag([-500, 0, 0], {"team": "red"}),
        DeathmatchFlag([500, 0, 0], {"team": "blue"}),
        DeathmatchSpawn([-600, 0, 0], {"team": "red"}),
        DeathmatchSpawn([600, 0, 0], {"team": "blue"}),
    ])
    plugin.on_play_start(logic)
    session = plugin._sessions[logic]
    human = session.fighters["player"]
    bot = next(f for f in session.fighters.values() if f.entity is not None)
    yield logic, session, human, bot


def test_ctf_steal_capture_drop_return_and_sprites(ctf):
    logic, session, human, bot = ctf
    assert human.team == "red" and bot.team == "blue"
    assert bot.entity.properties["monster_type"] == "flying"
    assert bot.entity.properties["variant"] == "variant1"
    game = session.game
    for flag in game.flags.values():
        assert Path(flag.entity.properties["sprite_path"]).is_file()
        assert flag.entity.properties["render_mode"] == "billboard"
    place(human, [500, 0, 0])
    game.step()
    assert human.carrying == "blue"
    assert game.flags["blue"].entity.pos == [500, 64, 0]
    assert game.flags["blue"].entity.properties['_sprite_overlay']
    assert game.flags["blue"].entity.properties['sprite_size'] == [64, 64]
    game.step()
    assert game.flags["blue"].entity.pos[1] == 64
    place(human, [-500, 0, 0])
    game.step()
    assert game.scores["red"] == 1 and human.captures == 1
    assert human.carrying is None and game.flags["blue"].pos == (500, 0, 0)
    assert not game.flags["blue"].entity.properties['_sprite_overlay']
    assert game.flags["blue"].entity.properties['sprite_size'] == [128, 128]
    place(human, [500, 0, 0]); game.step()
    place(human, [200, 0, 0])
    session.kill(human, bot)
    assert game.flags["blue"].dropped and game.flags["blue"].pos == (200, 0, 0)
    place(bot, [200, 0, 0]); game.step()
    assert not game.flags["blue"].dropped
    assert game.flags["blue"].pos == (500, 0, 0)


def test_ctf_cannot_capture_with_own_flag_missing_and_auto_return(ctf):
    logic, session, human, bot = ctf
    game = session.game
    place(human, [500, 0, 0]); game.step()
    place(bot, [-500, 0, 0]); game.step()
    place(human, [-500, 0, 0]); game.step()
    assert game.scores["red"] == 0 and human.carrying == "blue"
    place(bot, [100, 0, 100]); game.drop(bot)
    session.clock += game.return_delay + 0.1
    game.step()
    assert game.flags["red"].carrier is None and not game.flags["red"].dropped
    assert game.scores["red"] == 1


def test_ctf_capture_limit_ignores_fraglimit_and_restart_resets_flags(ctf):
    logic, session, human, bot = ctf
    session.limit = 1
    human.frags = 50
    session.check_frag_limit()
    assert not session.finished
    for _ in range(2):
        place(human, [500, 0, 0]); session.game.step()
        place(human, [-500, 0, 0]); session.game.step()
    assert session.finished and session.result_winner == "Red"
    session.restart()
    assert not session.finished and session.game.scores == {"red": 0, "blue": 0}
    assert all(not f.carrying for f in session.fighters.values())
    assert session.feet(human)[0] == -600 and session.feet(bot)[0] == 600


def test_teams_prevent_friendly_fire_and_teamkill_penalizes(arena):
    logic, session, human, bot = arena
    session.game.enabled = True
    session.game.mode = "teamdeathmatch"
    session.game.assign(human, "red"); session.game.assign(bot, "red")
    logic.combat_runtime._handle_shooting()
    assert bot.entity.properties["health"] == 100
    session.perceive(bot)
    assert bot.target is None
    session.game.friendly_fire = True
    for _ in range(4): logic.combat_runtime._handle_shooting()
    assert human.frags == -1 and session.game.scores["red"] == -1


def test_teamdeathmatch_team_frag_limit_and_ctf_bot_goals(arena, ctf):
    logic, session, human, bot = ctf
    assert session.game.objective(bot) == session.game.flags["red"].home
    session.game.touch(bot, session.game.flags["red"])
    assert session.game.objective(bot) == session.game.flags["blue"].home
    session.game.drop(bot)
    session.game.mode = "teamdeathmatch"; session.game.ctf = False
    session.limit = 1
    session.kill(bot, human)
    assert session.finished and session.result_winner == "Red"


def test_real_editor_play_hud_and_stop(fio_session):
    from types import SimpleNamespace
    from PyQt5.QtGui import QImage, QPainter
    from plugins.host import Event

    editor = fio_session("plugins/deathmatch/Deathmatch_Arena.json")
    bots = [t for t in editor.state.things if isinstance(t, DeathmatchBot)]
    positions = [list(t.pos) for t in bots]
    editor.start_play().step(120)
    plugin = get_manager().find_plugin("deathmatch")
    session = plugin._sessions[editor.logic]
    assert session.clock == pytest.approx(2, abs=0.05)
    assert len(session.snapshot["rows"]) == 5
    image = QImage(800, 600, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    try:
        plugin._draw_overlay(Event("render.overlay", dict(
            play_mode=True, viewport=SimpleNamespace(logic_thread=editor.logic),
            painter=painter, width=800, height=600)))
    finally:
        painter.end()
    assert image.pixelColor(15, 35).alpha() > 0
    editor.stop_play()
    assert editor.logic not in plugin._sessions
    assert [list(t.pos) for t in bots] == positions


def test_demo_bots_fight_over_a_full_round():
    load_plugins()
    manager = get_manager()
    plugin = manager.find_plugin("deathmatch")
    was_enabled = plugin.enabled
    state = EditorState()
    data = json.loads(Path(__file__).parents[1].joinpath("Deathmatch_Arena.json").read_text())
    state.load_from_data(data)
    manager.set_enabled(plugin, True)
    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player = Player(0, 850)
    logic.session_runtime.apply_play_mode(True)
    session = plugin._sessions[logic]
    try:
        for _ in range(6000):
            logic._tick_play_mode(1 / 60)
            if session.finished:
                break
        assert session.finished
        assert session.reason == "Frag limit"
        assert sum(f.deaths for f in session.fighters.values()) >= 15
    finally:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()
        manager.set_enabled(plugin, was_enabled)


def test_bot_heading_and_attack_reach_the_render_projection(arena):
    from engine.entity_table import EntityTable
    from engine.monster_facing import sprite_keys_for_view
    import numpy as np

    logic, session, human, bot = arena
    table = EntityTable()
    table.begin_frame([bot.entity], epoch=1)
    bot.actor.angle = math.pi / 2
    bot.shoot_until = session.clock + 1
    session.publish()
    table.begin_frame([bot.entity], epoch=1)
    assert bot.entity.properties['angle'] == pytest.approx(90)
    assert table.monster_angle[0] == pytest.approx(90)
    eye = np.asarray(bot.entity.pos) + [100, 0, 0]
    key = sprite_keys_for_view(table, np.array([0]), eye)[0]
    assert table.sprite_recipes()[key][0][1] == 'shoot_1.png'
    session.clock += 2
    session.publish()
    table.begin_frame([bot.entity], epoch=1)
    key = sprite_keys_for_view(table, np.array([0]), eye)[0]
    assert table.sprite_recipes()[key][0][1] == 'idle_1.png'


@pytest.mark.parametrize("sprite_height", [128, 192])
@pytest.mark.parametrize("floor_y", [0, 240])
def test_bot_billboard_feet_match_controller_after_respawn_and_movement(
        arena, sprite_height, floor_y):
    from engine.entity_table import EntityTable

    logic, session, human, bot = arena
    bot.entity.properties["sprite_height"] = sprite_height
    session.spawns = [((0, floor_y, 600), 0)]
    session.respawn(bot)
    assert session.feet(bot)[1] == pytest.approx(floor_y)
    table = EntityTable()

    def check_projection():
        table.begin_frame([bot.entity], epoch=1)
        feet = session.feet(bot)
        assert table.pos[0, 0] == pytest.approx(feet[0])
        assert table.pos[0, 2] == pytest.approx(feet[2])
        assert table.pos[0, 1] - table.sprite_size[0, 1] / 2 == pytest.approx(feet[1])

    check_projection()
    for _ in range(5):
        session.update_bot(bot, 1 / 60)
        check_projection()


@pytest.mark.parametrize('team', ['red', 'blue'])
def test_flag_light_stays_on_at_home_dropped_and_carried_then_cleans_up(ctf, team):
    from engine.entity_table import EntityTable
    logic, session, human, bot = ctf
    fighter = bot if team == 'red' else human
    flag = session.game.flags[team]
    light = flag.light
    assert light.properties['state'] == 'on' and not light.properties['hidden']
    assert light.pos == flag.entity.pos
    session.game.touch(fighter, flag)
    assert flag.light is light
    assert light is not None
    assert light.properties['state'] == 'on' and not light.properties['hidden']
    expected = [255, 35, 20] if team == 'red' else [40, 90, 255]
    assert light.properties['colour'] == expected
    place(fighter, [100, 0, 200])
    if fighter.entity is not None:
        session.sync_entity_position(fighter)
    session.game.step()
    centre_y = fighter.entity.properties['sprite_height']/2 if fighter.entity is not None else 64
    assert flag.entity.pos == light.pos == [100, centre_y, 200]
    table = EntityTable()
    table.begin_frame(logic.editor_state.things, epoch=1)
    row = table.slot_of_id[light.properties['id']]
    assert table.light_enabled[row]
    assert table.light_params[row].tolist() == [2, 320]
    assert table.light_color[row].tolist() == pytest.approx([v/255 for v in expected])
    flag_row = table.slot_of_id[flag.entity.properties['id']]
    assert table.sprite_overlay[flag_row]
    session.kill(fighter)
    assert flag.dropped and not flag.entity.properties['_sprite_overlay']
    assert light.properties['state'] == 'on' and not light.properties['hidden']
    assert light.pos == flag.entity.pos == [100, 64, 200]
    session.restart()
    assert flag.light is light  # Reuse, rather than accumulating lights per life.
    assert light.properties['state'] == 'on'
    assert light.pos == flag.entity.pos
    session.stop()
    assert light not in logic.editor_state.things
    assert '_sprite_overlay' not in flag.entity.properties
    assert flag.entity.properties['sprite_size'] == [128, 128]


def test_flag_textures_have_real_transparency_and_follow_editor_team(qt_app):
    from PyQt5.QtGui import QImage
    from engine.entity_table import sprite_candidates
    from plugins.deathmatch.entities import DeathmatchFlag
    flag = DeathmatchFlag()
    for team in ('red', 'blue'):
        flag.properties['team'] = team
        assert flag.get_sprite_path().endswith(f'{team}_flag.png')
        assert sprite_candidates(flag)[0][1] == f'{team}_flag.png'
        image = QImage(flag.get_sprite_path())
        assert not image.isNull() and image.hasAlphaChannel()
        assert image.width() == image.height()
        assert image.pixelColor(0, 0).alpha() == 0
        colour = image.pixelColor(image.width()//3, image.height()//3)
        assert colour.alpha() >= 250
        assert (colour.red() > colour.blue()) if team == 'red' else (colour.blue() > colour.red())


def test_dflaglight_switches_shadows_without_extinguishing_flags(ctf):
    logic, session, human, bot = ctf
    assert not session.game.flag_shadows
    assert command(logic, 'dflaglight') == 'Flag light shadows: on.'
    assert session.game.flag_shadows
    for flag in session.game.flags.values():
        assert flag.light.properties['casts_shadows']
        assert flag.light.properties['state'] == 'on'
    session.restart()
    assert all(f.light.properties['casts_shadows'] for f in session.game.flags.values())
    assert command(logic, 'dflaglight', 'off') == 'Flag light shadows: off.'
    assert not session.game.flag_shadows
    assert all(not f.light.properties['casts_shadows'] for f in session.game.flags.values())
    assert all(f.light.properties['state'] == 'on' for f in session.game.flags.values())
    assert command(logic, 'dflaglight', 'invalid') == 'Usage: dflaglight [on|off]'
