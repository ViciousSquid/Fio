"""The shipped plugins on the player: Deathmatch, Bigworld and Tidy.

Runs in the staged Android runtime (``python -m player.tools.stage_runtime``),
against the real plugins, engine and plugin manager -- no plugin fakes.
"""
import configparser
import json
import os
from pathlib import Path
import time

import pytest
from PIL import Image

from player.input.state import (ACTION_RESTART, ACTION_SCORES, ACTION_WEAPON, InputState)
from player.input.touch import TouchControls
from player.menu import hit_test, layout
from player.plugin_ui import arena, bigworld, scoreboard
from player.plugin_ui.lobby import Lobby, broadcast_destinations, valid_address
from player.plugin_ui.menus import PluginMenus
from player.runtime.fiopak import FioPackage
from player.runtime.plugin_assets import PluginAssetPackage, plugin_asset, plugins_root
from player.tests.fixtures import build_package
from player.world import PlayerWorld

ROOT = Path(__file__).resolve().parents[2]
DEATHMATCH = ROOT / 'plugins/deathmatch'


def arena_map(name='Deathmatch_Arena.json'):
    return json.loads((DEATHMATCH / name).read_text())


def run(world, seconds, inp=None):
    inp = inp or idle()
    for _ in range(int(seconds * 60)):
        world.step(1 / 60, inp, 0, 0)


def idle():
    inp = InputState()
    inp.begin_frame()
    return inp


@pytest.fixture
def world():
    opened = PlayerWorld(arena_map())
    yield opened
    opened.close()


# -- Deathmatch -----------------------------------------------------------

def test_an_exported_arena_runs_a_match_on_the_player(world):
    session = arena.session(world)
    assert session is not None and session.game.mode == 'deathmatch'
    assert world.state.get_level_data() == arena_map()
    run(world, 20)
    state = arena.snapshot(world)
    names = [row[0] for row in state['rows']]
    assert 'You' in names and len(names) == 5
    assert sum(row[1] for row in state['rows']) > 0, 'bots never fragged anyone'
    assert 0 < state['remaining'] < 300   # the demo's five-minute clock runs


def test_match_controls_run_the_plugins_own_console_commands(world):
    assert arena.add_bot(world, 'hard', 'auto') == 'Added Bot 1 (hard).'
    assert 'Bot 1' in [row[0] for row in arena.snapshot(world)['rows']]
    assert arena.remove_bot(world) == 'Removed Bot 1.'
    assert arena.frag_limit(world, 7) == 'Frag limit: 7.'
    assert arena.snapshot(world)['limit'] == 7
    run(world, 2)
    session = arena.session(world)
    assert session.clock > 1.5
    assert arena.restart(world) == 'Restarting the match.'
    run(world, .1)
    assert session.clock < .5


def test_restart_button_only_after_a_match_ends(world):
    assert not arena.can_restart(world)
    arena.session(world).finish('Map I/O')
    assert arena.can_restart(world)
    inp = idle()
    inp.set_button(ACTION_RESTART, True)
    from player.app import FioPlayerApp
    app = FioPlayerApp()
    app.world, app._paused = world, False
    app._plugin_frame(1 / 60, inp)
    run(world, .1)
    assert not arena.snapshot(world)['finished']


def test_spectator_menu_actions(world):
    run(world, 1)
    assert arena.spectate(world, 'free') == 'Spectating: free camera.'
    assert arena.snapshot(world)['spectating']
    reply = arena.spectate(world, 'next')
    assert reply.startswith('Watching ')
    assert arena.snapshot(world)['watching'] == reply.removeprefix('Watching ').rstrip('.')
    assert arena.spectate(world, 'off') == 'Joined the match.'
    assert not arena.snapshot(world)['spectating']


def test_other_spectators_are_drawn_as_their_glasses(world):
    session = arena.session(world)
    bot = next(f for f in session.fighters.values() if f.entity is not None)
    bot.glasses_style = 'pixel_shades'
    session.spectators.set(bot, True)
    glasses = arena.spectator_glasses(world)
    assert glasses == [(session.eye(bot), 'pixel_shades')]


def test_ctf_flag_shadow_toggle():
    world = PlayerWorld(arena_map('CTF_Arena.json'))
    try:
        assert arena.snapshot(world)['mode'] == 'ctf'
        before = arena.flag_shadows_enabled(world)
        arena.flag_shadows(world, not before)
        assert arena.flag_shadows_enabled(world) is (not before)
    finally:
        world.close()


def test_weapon_button_cycles_owned_weapons(world):
    assert world.owned_weapons() == [1, 2]
    combat = world.logic.combat_runtime
    first = combat.active_weapon
    inp = idle()
    inp.set_button(ACTION_WEAPON, True)
    world.step(1 / 60, inp, 0, 0)
    assert combat.active_weapon != first
    inp.begin_frame()                       # release, then a fresh press
    world.step(1 / 60, inp, 0, 0)
    inp.begin_frame()
    inp.set_button(ACTION_WEAPON, True)
    world.step(1 / 60, inp, 0, 0)
    assert combat.active_weapon == first


def test_situational_touch_buttons():
    controls = TouchControls()
    base = [b.action for b in controls.buttons]
    controls.set_context(weapons=2, arena=True, can_restart=True)
    assert [b.action for b in controls.buttons] == base + ['weapon', 'scores', 'restart']
    # A finger holding Restart is released when the button goes away.
    restart = controls.RESTART_BUTTON
    state = InputState()
    state.begin_frame()
    controls.apply(state, {7: (restart.cx * 1000, restart.cy * 500)}, 1000, 500)
    assert state.is_down('restart')
    controls.set_context(weapons=1)
    state.begin_frame()
    controls.apply(state, {7: (restart.cx * 1000, restart.cy * 500)}, 1000, 500)
    assert not state.is_down('restart') and [b.action for b in controls.buttons] == base


def test_scoreboard_draws_the_plugin_snapshot(world):
    run(world, 3)
    state = arena.snapshot(world)
    assert scoreboard.prompt('DEATHMATCH | 4:57 | TAB: scoreboard').endswith('Hold SCORES')
    assert hash(scoreboard.key(state, False)) != hash(scoreboard.key(state, True))
    compact, full = (Image.new('RGBA', (1280, 720)) for _ in range(2))
    scoreboard.paint(compact, state)
    scoreboard.paint(full, state, full=True)
    assert compact.getbbox() and full.getbbox()
    assert compact.getbbox()[0] == 12          # leaders, top left
    assert full.getbbox() != compact.getbbox()  # the whole table, centred


class _App:
    def __init__(self, world, package=None):
        self.world, self.package = world, package
        self.text_input = False
        self.hosted = self.left = 0

    def set_text_input(self, active):
        self.text_input = active

    def host_lan(self, lobby):
        self.hosted += 1
        return 'Hosting on test.'

    def leave_lan(self):
        self.left += 1
        return 'Left the LAN game.'


def test_pause_pages_reach_every_match_control(world):
    menus, app = PluginMenus(), _App(world)
    root = menus.current(app)
    keys = [item.key for item in root.items]
    assert keys[:4] == ['resume', 'page:match', 'page:spectate', 'page:lan']
    assert menus.activate('page:match', app) is None
    match = menus.current(app)
    assert {'restart', 'removebot', 'addbot:easy', 'addbot:normal', 'addbot:hard',
            'frag:-', 'frag:+', 'page:spectate', 'back'} <= {i.key for i in match.items}
    for key, _label, (x, y, w, h), _style in layout(match, 1280, 720):
        assert hit_test((x + w / 2, y + h / 2), 1280, 720, True, match) == key
    menus.activate('addbot:normal', app)
    assert menus.message == 'Added Bot 1 (normal).'
    menus.activate('frag:+', app)
    assert arena.snapshot(world)['limit'] > 15
    assert menus.activate('restart', app) == 'resume'
    menus.activate('page:spectate', app)
    assert menus.current(app).title == 'Spectator'
    menus.activate('back', app)
    assert menus.page == 'match'


def test_lan_page_fields_and_settings(world):
    menus, app = PluginMenus(), _App(world, package=object())
    menus.page = 'lan'
    menus.activate('edit:name', app)
    assert app.text_input and menus.editing == 'name'
    assert menus.text([('backspace',)] * 6 + [('text', 'Phone 1'), ('enter',)])
    assert menus.lobby.name == 'Phone 1' and menus.editing is None
    menus.activate('mode', app)
    assert menus.lobby.mode == 'ctf'
    assert 'capture_limit' in {i.key for i in menus.current(app).items}
    assert menus.activate('host', app) == 'resume' and app.hosted == 1
    menus.lobby.address = ''
    menus.activate('join', app)
    assert menus.lobby.status == "Enter the host's LAN address."


def test_hosting_requires_an_arena_and_ctf_flags():
    lobby = Lobby()
    with pytest.raises(ValueError, match='Match Settings'):
        lobby.prepare_host({'things': [{'type': 'playerstart'}]})
    lobby.mode = 'ctf'
    with pytest.raises(ValueError, match='red and one blue'):
        lobby.prepare_host(arena_map())
    lobby.mode, lobby.frag_limit, lobby.team = 'deathmatch', 25, 'blue'
    data = lobby.prepare_host(arena_map())
    settings = next(t for t in data['things'] if t['type'] == 'deathmatchsettings')['properties']
    assert (settings['game_type'], settings['frag_limit'], settings['player_team']) == ('deathmatch', 25, 'blue')


def test_discovery_reaches_the_subnet_broadcast():
    assert broadcast_destinations('192.168.1.23') == ('255.255.255.255', '192.168.1.255', '127.0.0.1')
    assert broadcast_destinations('10.1.2.3', '255.255.0.0')[1] == '10.1.255.255'
    assert valid_address('192.168.1.5') and valid_address('arena.local')
    assert not valid_address('') and not valid_address('a b')


@pytest.fixture
def arena_package(tmp_path):
    path = tmp_path / 'arena.fiopak'
    path.write_bytes(build_package({'title': 'Arena', 'map_path': 'maps/Deathmatch_Arena.json',
                                    'plugins': ['deathmatch']},
                                   {'maps/Deathmatch_Arena.json': arena_map()}))
    return path


class _Host:
    width, height = 1280, 720

    def __init__(self):
        self.ui_events, self.text_events = [], []
        self.input_state = InputState()
        self.touch = TouchControls()

    def set_mouse_captured(self, captured):
        pass


def _player(path):
    from player.app import FioPlayerApp
    app = FioPlayerApp(FioPackage.open(str(path)))
    app.host = _Host()
    app._load_level(app.package.start_map_path())
    return app


def _frames(apps, seconds):
    for _ in range(int(seconds * 60)):
        for app in apps:
            app.host.input_state.begin_frame()
            app._on_frame(1 / 60, app.host.input_state)
        time.sleep(.002)


def test_two_players_host_and_join_a_lan_arena(arena_package, monkeypatch):
    """Real loopback sockets: one player hosts the open world, one joins."""
    from plugins.deathmatch import lan
    opened = []

    def ephemeral_host(self):
        transport = lan.LanHost(self.game_name, self.mode, address='127.0.0.1', port=0, discovery_port=0)
        self.port, self.discovery_port = transport.port, transport.discovery_port
        opened.append(transport)
        return transport

    monkeypatch.setattr(Lobby, 'open_host', ephemeral_host)
    host, guest = _player(arena_package), _player(arena_package)
    try:
        host.menus.lobby.name = 'Tablet'
        reply = host.host_lan(host.menus.lobby)
        assert reply.startswith('Hosting'), reply
        assert arena.is_host(host.world)
        guest.menus.lobby.name = 'Phone'
        assert guest.menus.lobby.join('127.0.0.1', opened[0].port)
        _frames((host, guest), 3)
        assert arena.is_client(guest.world), guest.menus.lobby.status
        # The host names each player; the guest sees itself as named.
        names = {row[0] for row in arena.snapshot(guest.world)['rows']}
        assert {'Phone', 'Tablet'} <= names
        assert 'Phone' in {row[0] for row in arena.snapshot(host.world)['rows']}
        # Match controls belong to the host.
        assert arena.restart(guest.world) == 'The LAN host controls restarts.'
        assert guest.menus.current(guest).items  # pages build while joined
        # The host pausing keeps the game flowing, paused, for the guest.
        host._paused = True
        _frames((host, guest), 1)
        assert guest.world.logic.session_runtime.world_paused
        host._paused = False
        _frames((host, guest), 1)
        assert not guest.world.logic.session_runtime.world_paused
        assert guest.leave_lan() == 'Left the LAN game.'
        _frames((host,), 1)
        assert 'Phone' not in {row[0] for row in arena.snapshot(host.world)['rows']}
    finally:
        for app in (guest, host):
            app._on_shutdown()


def test_joining_needs_an_open_world():
    menus, app = PluginMenus(), _App(None, package=None)
    menus.page = 'lan'
    menus.lobby.address = '192.168.1.9'
    menus.activate('join', app)
    assert 'exported from desktop Fio' in menus.message
    assert menus.lobby.pending is None


# -- Plugin-owned assets ---------------------------------------------------

def test_plugin_owned_art_comes_from_the_installed_plugins(tmp_path):
    package = FioPackage.from_bytes(build_package({'map_path': 'maps/a.json'}, {'maps/a.json': {}},
                                                  {'assets/textures/floor.png': b'world'}))
    assets = PluginAssetPackage(package)
    flag = plugins_root() / 'deathmatch/sprites/red_flag.png'
    assert assets.read_asset(str(flag)) == flag.read_bytes()        # the plugin's own path
    assert assets.read_asset('plugins/tidy/assets/book.obj') == (plugins_root() / 'tidy/assets/book.obj').read_bytes()
    assert assets.has_asset('plugins/tidy/assets/covers/cover_01.png')
    assert assets.read_asset('floor.png') == b'world'                # the world always wins
    assert assets.start_map_path() == 'maps/a.json'                  # everything else delegates
    for refused in ('plugins/deathmatch/plugin.py', str(plugins_root() / 'manager.py'),
                    'plugins/../player/app.py', str(tmp_path / 'x.png'), 'assets/sprites/x.png'):
        assert plugin_asset(refused) is None, refused
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'x')
    link = plugins_root() / 'deathmatch/sprites/_escape_test.png'
    try:
        os.symlink(outside, link)
        assert plugin_asset(str(link)) is None
    except OSError:
        pass
    finally:
        link.unlink(missing_ok=True)


# -- Bigworld and Tidy --------------------------------------------------------

def _bigworld_map():
    brushes = [{'id': f'b{x}_{z}', 'pos': [x * 512.0, 0.0, z * 512.0], 'size': [256.0, 32.0, 256.0],
                'textures': {}} for x in range(-8, 9) for z in range(-8, 9)]
    return {'version': 3, 'brushes': brushes, 'things': [
        {'type': 'PlayerStart', 'pos': [0, 64, 0], 'properties': {}},
        {'type': 'bigworldsettings', 'pos': [0, 0, 0],
         'properties': {'type': 'bigworldsettings', 'show_cell_debug': True,
                        'activation_radius': 1024.0, 'deactivation_radius': 1280.0}}]}


def test_bigworld_streams_cells_around_the_player_and_shows_its_panel():
    import glm
    world = PlayerWorld(_bigworld_map())
    try:
        run(world, .5)
        panel = bigworld.state(world)
        assert panel is not None
        rows, cells = panel
        stats = dict(rows)
        assert stats['Player Cell'] == '0, 0'
        active = int(stats['Active Brushes'])
        assert 0 < active < len(_bigworld_map()['brushes'])
        assert any(is_active for _dx, _dz, is_active in cells)
        world.player.pos = glm.vec3(3500, 64, 3500)
        run(world, .5)
        assert dict(bigworld.state(world)[0])['Player Cell'] == '6, 6'
        image = Image.new('RGBA', (1280, 720))
        bigworld.paint(image, bigworld.state(world))
        assert image.getbbox()[0] == 12
    finally:
        world.close()


def test_tidy_plays_on_the_player():
    data = json.loads((ROOT / 'plugins/tidy/Tidy_Test.json').read_text())
    world = PlayerWorld(data)
    try:
        run(world, 1)
        assert world.logic.plugins.find_plugin('tidy').enabled
        assert world.hud.splitlines()[-1].startswith('Tidied: 0/')
        book = next(t for t in world.state.things if 'book' in str(t.properties.get('model_path', '')))
        assert PluginAssetPackage(None).has_asset(book.properties['model_path'])
    finally:
        world.close()


def test_the_apk_ships_all_three_plugins():
    """Buildozer's source rules package every shipped plugin, code and data.

    Mirrors Buildozer's copy rules: lower-cased directory prefixes excluded
    relative to ``source.dir``, files kept by extension.
    """
    spec = configparser.ConfigParser()
    spec.read(ROOT / 'player/buildozer.spec')
    exts = {e.strip() for e in spec['app']['source.include_exts'].split(',')}
    excluded = [d.strip().lower().rstrip('/') + '/' for d in spec['app']['source.exclude_dirs'].split(',')]
    shipped = set()
    for path in (ROOT / 'plugins').rglob('*'):
        relative = path.relative_to(ROOT).as_posix()
        if (path.is_file() and path.suffix.lstrip('.') in exts and '__pycache__' not in relative
                and not any(relative.lower().startswith(d) for d in excluded)):
            shipped.add(relative)
    for plugin in ('bigworld', 'deathmatch', 'tidy'):
        assert f'plugins/{plugin}/__init__.py' in shipped
        assert f'plugins/{plugin}/plugin.py' in shipped
    assert {'plugins/manager.py', 'plugins/api.py', 'plugins/host.py'} <= shipped
    assert {'plugins/deathmatch/sprites/red_flag.png', 'plugins/deathmatch/sprites/blue_flag.png',
            'plugins/tidy/assets/book.obj', 'plugins/tidy/assets/book.mtl',
            'plugins/bigworld/assets/bigworldsettings.png'} <= shipped
    permissions = {p.strip() for p in spec['app']['android.permissions'].split(',')}
    assert {'INTERNET', 'ACCESS_WIFI_STATE', 'CHANGE_WIFI_MULTICAST_STATE'} <= permissions


def test_back_steps_out_of_plugin_pages_before_resuming(world):
    from player.app import FioPlayerApp
    from player.input.state import ACTION_PAUSE
    app = FioPlayerApp()
    app.host, app.world, app._paused = _Host(), world, True
    app.menus.activate('page:match', app)
    app.menus.activate('page:spectate', app)

    def back():
        inp = app.host.input_state
        inp.begin_frame()
        app._on_frame(1 / 60, inp)          # release
        inp.begin_frame()
        inp.set_button(ACTION_PAUSE, True)
        app._on_frame(1 / 60, inp)

    back()
    assert (app._paused, app.menus.page) == (True, 'match')
    back()
    assert (app._paused, app.menus.page) == (True, 'root')
    back()
    assert not app._paused
