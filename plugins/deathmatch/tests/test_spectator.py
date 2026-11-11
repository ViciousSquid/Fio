"""Spectator membership, cameras, appearances and actual loopback sessions."""
import pytest
import glm

from engine.glasses import world_glasses
from engine.threaded_game_state import RenderState
from plugins.manager import get_manager
from plugins.deathmatch.tests.test_deathmatch import arena, ctf, place, pickup, command
from plugins.deathmatch.tests.test_lan import linked, wait_until

pytestmark = pytest.mark.qt


def test_free_camera_is_not_a_combatant_and_rejoin_respawns(arena):
    logic, session, human, bot = arena
    before = (human.frags, human.deaths, bot.frags)
    assert command(logic, 'spectate', 'free') == 'Spectating: free camera.'
    assert human.spectator and not human.alive
    assert not human.actor.physics_enabled and not logic.player_runtime.player_dead
    original = glm.vec3(human.actor.pos)
    logic.player_runtime.update_primary(.1, {0x20, 0x57}, 0, 0)
    assert human.actor.pos.y > original.y and human.actor.pos.z > original.z
    prop = pickup(logic, list(session.eye(human)), collect_type='health', collect_value=50)
    logic.player_runtime.player_health = 20
    assert not logic.prop_runtime.collect_prop(prop)
    session.kill(human, bot)
    assert (human.frags, human.deaths, bot.frags) == before
    assert not session._can_damage(human)
    for _ in range(360):
        session.step(1/60)
    assert human.spectator and not human.alive  # No death-screen auto respawn.
    assert command(logic, 'spectate', 'off') == 'Joined the match.'
    assert human.alive and not human.spectator and human.actor.physics_enabled
    assert session.health(human) == 100


def test_follow_death_fallback_round_restart_and_stop(arena):
    logic, session, human, bot = arena
    logic.camera.camera_mode = 'Overhead'
    command(logic, 'spectate', bot.name)
    place(bot, [400, 0, 200], angle=.6)
    bot.actor.pitch = .3
    session.spectators.follow(human)
    assert human.actor.pos == bot.actor.pos
    assert human.actor.angle == .6 and human.actor.pitch == .3
    session.restart()
    assert human.spectator and not human.alive
    session.kill(bot)
    assert not session.spectators.follow(human)
    assert human.spectator_target is None
    assert 'free camera' in session.spectators.cycle()
    session.stop()
    assert human.actor.physics_enabled and logic.camera.camera_mode == 'Overhead'


def test_entering_spectator_drops_flag_and_cannot_take_another(ctf):
    logic, session, human, bot = ctf
    flag = session.game.flags['blue']
    session.game.touch(human, flag)
    assert human.carrying == 'blue'
    session.spectators.choose(True)
    assert human.carrying is None and flag.dropped
    session.game.touch(human, flag)
    assert human.carrying is None and flag.light.properties['state'] == 'on'


@pytest.mark.parametrize('show_normal', [False, True])
def test_spectator_glasses_are_always_visible_and_keep_each_wearers_style(show_normal):
    state = RenderState()
    state.player_pos = glm.vec3(0, 50, 0)
    state.local_spectator = True
    state.spectator_glasses = (((200, 90, 0), 'cateye_pink'), ((-200, 90, 0), 'pixel_shades'))
    positions, sprites, remotes, styles, active = world_glasses(state, show_normal, 'sunnies_green')
    assert active and len(positions) == 3
    assert sprites == ('Glasses:sunnies_green', 'Glasses:cateye_pink', 'Glasses:pixel_shades')
    assert remotes == ((200, 90, 0), (-200, 90, 0))
    assert styles == sprites[1:]
    state.local_spectator = False
    state.spectator_glasses = ()
    assert bool(world_glasses(state, show_normal, 'classic')[0]) is show_normal


def test_hidden_spectator_body_publishes_glasses_not_a_death_screen(arena):
    from engine.entity_table import EntityTable
    logic, session, human, bot = arena
    table = EntityTable()
    table.begin_frame([bot.entity], epoch=1)
    assert not table.hidden[0]
    session.spectators.set(bot, True)
    table.begin_frame([bot.entity], epoch=1)
    assert table.hidden[0]
    bot.glasses_style = 'pixel_shades'
    session.spectators.choose(True)
    logic.render_runtime.prepare_render_state()
    state = logic.game_state.get_write_state()
    assert state.local_spectator and not state.player_dead
    assert state.active_weapon is None
    assert bot.entity.properties['hidden'] and bot.entity.properties['dead']
    assert state.spectator_glasses == ((session.eye(bot), 'pixel_shades'),)
    session.spectators.set(bot, False)
    table.begin_frame([bot.entity], epoch=1)
    assert not table.hidden[0]


def test_live_menu_follow_and_rejoin_stop_the_refresh_timer(arena, main_window, qt_app):
    logic, session, human, bot = arena
    plugin = get_manager().find_plugin('deathmatch')
    plugin._open_spectator(main_window, logic)
    menu = main_window._spectator_menu
    assert menu.isVisible() and menu.timer.interval() == 500
    assert menu.players.count() == 1
    menu.follow.click()
    assert human.spectator and human.spectator_target == bot.key
    assert not menu.isVisible() and not menu.timer.isActive()
    plugin._open_spectator(main_window, logic)
    menu.join.click()
    assert human.alive and not human.spectator


def test_lan_spectator_free_flight_glasses_follow_and_rejoin(linked):
    hl, hs, cl, cs, host, client, remote, pump = linked
    cl.player_runtime.glasses_style = 'cateye_pink'
    cs.spectators.choose(True)
    wait_until(lambda: remote.spectator and cs.fighters['player'].spectator, pump)
    assert not remote.alive and remote.entity.properties['hidden']
    assert remote.glasses_style == 'cateye_pink'
    start_y = remote.actor.pos.y
    cl.game_state.set_keys({0x20})
    pump(30)
    cl.game_state.set_keys(set())
    assert remote.actor.pos.y > start_y + 20
    hl.render_runtime.prepare_render_state()
    assert any(style == 'cateye_pink' for pos, style in hl.game_state.get_write_state().spectator_glasses)
    cs.spectators.choose(True, 'host')
    wait_until(lambda: remote.spectator_target == 'player', pump)
    assert remote.actor.pos == hs.fighters['player'].actor.pos
    cs.spectators.choose(False)
    wait_until(lambda: not remote.spectator and not cs.fighters['player'].spectator and remote.alive, pump)
    assert remote.actor.physics_enabled and not remote.entity.properties['hidden']
    assert not cl.player_runtime.player_dead


def test_pause_menu_opens_live_spectator_controls_and_resumes_play(fio_session):
    from editor.things import PlayerStart
    from plugins.deathmatch.entities import DeathmatchSettings, DeathmatchBot, DeathmatchSpawn
    from tests.helpers.worlds import level_data, room
    from tests.editor.test_pause_menu import _choose
    manager = get_manager()
    plugin = manager.find_plugin('deathmatch')
    was_enabled = plugin.enabled
    manager.set_enabled(plugin, True)
    try:
        game = fio_session(level_data(brushes=room(), things=[
            PlayerStart([0, 50, 0]), DeathmatchSettings(properties={'frag_limit': 0, 'time_limit': 0}),
            DeathmatchBot([300, 0, 300]), DeathmatchSpawn([0, 0, 0])]))
        game.start_play()
        game.step(5)
        session = plugin._sessions[game.logic]
        game.view.pause.open_pause_menu()
        assert game.logic.session_runtime.pause_menu_open
        _choose(game.view.pause_menu, 'Spectator')
        menu = game.window._spectator_menu
        assert menu.isVisible() and not game.view.pause.pause_menu_active
        assert not game.logic.session_runtime.pause_menu_open
        menu.free.click()
        assert session.fighters['player'].spectator
        before = game.logic.player_runtime.player.pos.y
        game.step(5, keys={0x20})
        assert game.logic.player_runtime.player.pos.y > before
        game.view.pause.open_pause_menu()
        _choose(game.view.pause_menu, 'Spectator')
        menu.join.click()
        assert session.fighters['player'].alive and not session.fighters['player'].spectator
        assert not game.logic.session_runtime.pause_menu_open
    finally:
        manager.set_enabled(plugin, was_enabled)
