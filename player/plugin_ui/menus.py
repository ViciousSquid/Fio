"""Pause-menu pages for the shipped plugins' player controls.

Desktop Fio offers these through its debug console, the Escape menu and Qt
dialogs; the player offers the same choices as touch pages:

* **Match** -- restart, add/remove bots, frag limit, CTF flag shadows
  (``addbot``, ``removebot``, ``fraglimit``, ``dflaglight``, Restart);
* **Spectator** -- free camera, follow a participant, cycle, join the match
  (``spectate``, the desktop Spectator dialog);
* **LAN multiplayer** -- discover, join and host (the desktop LAN dialog).

Pages are pure data (:mod:`player.menu`); :meth:`PluginMenus.activate`
performs a tapped item and returns any action the app itself must take.
"""
from __future__ import annotations

from ..menu import Item, Page, root_page
from . import arena
from .lobby import Lobby, local_address

MAX_GAMES = 6
MAX_WATCH = 8


class PluginMenus:
    def __init__(self):
        self.page = 'root'
        self.lobby = Lobby()
        self.editing = None        # lobby text field with the soft keyboard
        self.message = ''
        self.bot_team = 'auto'

    # -- navigation ----------------------------------------------------
    def reset(self):
        self.page = 'root'
        self.editing = None
        self.message = ''

    def back(self):
        self.editing = None
        self.message = ''
        self.page = 'match' if self.page == 'spectate' else 'root'

    # -- pages ---------------------------------------------------------
    def current(self, app):
        builder = {'match': self._match, 'spectate': self._spectate, 'lan': self._lan}.get(self.page)
        if builder is None or (self.page in ('match', 'spectate') and arena.session(app.world) is None):
            self.page = 'root'
            return self._root(app)
        return builder(app)

    def _root(self, app):
        world = app.world
        extra = []
        if arena.session(world) is not None:
            extra.append(Item('page:match', 'Match…'))
            extra.append(Item('page:spectate', 'Spectator…'))
        if world is not None and arena.installed():
            extra.append(Item('page:lan', 'LAN multiplayer…'))
        page = root_page(world is not None, extra)
        return Page(page.title, page.items, self.message)

    def _match(self, app):
        world = app.world
        state = arena.snapshot(world)
        client = arena.is_client(world)
        mode = state.get('mode', 'deathmatch')
        host_only = 'disabled' if client else 'button'
        team_mode = mode in ('ctf', 'teamdeathmatch')
        items = [Item('restart', 'Restart match', host_only),
                 Item('removebot', 'Remove last bot', host_only)]
        for skill in arena.SKILLS:
            items.append(Item(f'addbot:{skill}', f'Add {skill} bot', host_only))
        if team_mode:
            items.append(Item('botteam', f'New bot team: {self.bot_team}', host_only))
        limit = state.get('limit') or 0
        items.append(Item('frag:-', f'Frag limit -  ({limit or "unlimited"})', host_only))
        items.append(Item('frag:+', 'Frag limit +', host_only))
        if mode == 'ctf':
            shadows = arena.flag_shadows_enabled(world)
            items.append(Item('flagshadows', f'Flag light shadows: {"on" if shadows else "off"}'))
        items.append(Item('page:spectate', 'Spectator…'))
        items.append(Item('back', 'Back'))
        status = self.message or ('The LAN host controls the match.' if client else '')
        return Page(f'Match - {mode.upper()}', tuple(items), status, columns=2)

    def _spectate(self, app):
        world = app.world
        state = arena.snapshot(world)
        spectating = state.get('spectating')
        items = [Item('spectate:free', 'Free camera', 'selected' if spectating and not state.get('watching') else 'button'),
                 Item('spectate:prev', 'Previous player'),
                 Item('spectate:next', 'Next player'),
                 Item('spectate:off', 'Join match', 'button' if spectating else 'disabled')]
        watching = state.get('watching')
        for key, name, team, alive, spectator in arena.watchable(world)[:MAX_WATCH]:
            if key == 'player' or spectator:
                continue
            label = (f'[{team[0].upper()}] ' if team else '') + str(name)
            style = 'selected' if name == watching else ('button' if alive else 'disabled')
            items.append(Item('watch:' + str(name), 'Watch ' + label, style))
        items.append(Item('back', 'Back'))
        status = self.message or ('Free camera: move stick to fly · Jump up · Crouch down · Sprint faster\n'
                                  'Spectators appear to other players as their glasses.')
        return Page('Spectator', tuple(items), status, columns=2)

    def _lan(self, app):
        lobby = self.lobby
        world = app.world
        items = []
        typing = lambda field: 'selected' if self.editing == field else 'field'
        cursor = lambda field: '_' if self.editing == field else ''
        items.append(Item('edit:name', f'Name: {lobby.name}{cursor("name")}', typing('name')))
        items.append(Item('team', f'Team: {lobby.team}'))
        if arena.session(world) is not None and arena.session(world).network is not None:
            items.append(Item('leave', 'Leave LAN game'))
        items.append(Item('refresh', 'Refresh LAN games'))
        for index, ((address, port), offer) in enumerate(lobby.games()[:MAX_GAMES]):
            label = (f"Join {offer.get('name', 'Arena')} - {offer.get('mode', 'deathmatch')} - "
                     f"{offer.get('players', 0)}/{offer.get('capacity', 8)}")
            items.append(Item(f'game:{index}', label[:60]))
        items.append(Item('edit:address', f'Address: {lobby.address}{cursor("address")}', typing('address')))
        items.append(Item('join', 'Join address'))
        items.append(Item('edit:game_name', f'Game: {lobby.game_name}{cursor("game_name")}', typing('game_name')))
        items.append(Item('mode', f'Type: {lobby.mode_label()}'))
        if lobby.mode == 'ctf':
            items.append(Item('capture_limit', f'Capture limit: {lobby.capture_limit or "unlimited"}'))
        else:
            items.append(Item('frag_limit', f'Frag limit: {lobby.frag_limit or "unlimited"}'))
        items.append(Item('host', 'Host this world'))
        items.append(Item('back', 'Back'))
        lines = [self.message or lobby.status,
                 f'This device: {local_address()} · TCP 27961 · discovery UDP 27960',
                 'Everyone needs the same world, exported from desktop Fio, open before joining.']
        return Page('LAN multiplayer', tuple(items), '\n'.join(line for line in lines if line), columns=2)

    # -- actions -------------------------------------------------------
    def activate(self, key, app):
        """Perform *key*; return ``'resume'``/``'open'``/``'exit'`` for the app."""
        if key in ('resume', 'open', 'exit'):
            self.editing = None
            return key
        if key.startswith('page:'):
            self.page = key.split(':', 1)[1]
            self.editing = None
            self.message = ''
            if self.page == 'lan' and not self.lobby.offers:
                self.lobby.refresh()
            return None
        if key == 'back':
            self.back()
            return None
        if self.page == 'lan' or key.startswith('edit:'):
            return self._lan_action(key, app)
        world = app.world
        if key == 'restart':
            self.message = arena.restart(world)
            return 'resume' if self.message.startswith('Restarting') else None
        if key == 'removebot':
            self.message = arena.remove_bot(world)
        elif key.startswith('addbot:'):
            self.message = arena.add_bot(world, key.split(':', 1)[1], self.bot_team)
        elif key == 'botteam':
            self.bot_team = arena.TEAMS[(arena.TEAMS.index(self.bot_team) + 1) % len(arena.TEAMS)]
        elif key.startswith('frag:'):
            from .lobby import FRAG_STEPS
            current = arena.snapshot(world).get('limit') or 0
            larger = [step for step in FRAG_STEPS if step > current]
            smaller = [step for step in FRAG_STEPS if step < current]
            value = (larger[0] if larger else current) if key.endswith('+') else (smaller[-1] if smaller else 0)
            self.message = arena.frag_limit(world, value)
        elif key == 'flagshadows':
            self.message = arena.flag_shadows(world, not arena.flag_shadows_enabled(world))
        elif key.startswith('spectate:'):
            self.message = arena.spectate(world, key.split(':', 1)[1])
        elif key.startswith('watch:'):
            self.message = arena.spectate(world, key.split(':', 1)[1])
        return None

    def _lan_action(self, key, app):
        lobby = self.lobby
        if key.startswith('edit:'):
            field = key.split(':', 1)[1]
            self.editing = None if self.editing == field else field
            app.set_text_input(self.editing is not None)
            return None
        if self.editing is not None:
            self.editing = None
            app.set_text_input(False)
        self.message = ''
        if key in ('team', 'mode', 'frag_limit', 'capture_limit'):
            lobby.cycle(key)
        elif key == 'refresh':
            lobby.refresh()
        elif key.startswith('game:'):
            games = lobby.games()
            index = int(key.split(':', 1)[1])
            if index < len(games):
                (address, port), _offer = games[index]
                self._join(app, address, port)
        elif key == 'join':
            self._join(app, lobby.address, None)
        elif key == 'host':
            self.message = app.host_lan(lobby)
            return 'resume' if self.message.startswith('Hosting') else None
        elif key == 'leave':
            self.message = app.leave_lan()
        return None

    def _join(self, app, address, port):
        if app.package is None:
            self.message = 'Open the world exported from desktop Fio first, then join.'
            return
        self.lobby.join(address, port)

    def text(self, events):
        """Feed soft-keyboard events to the field being edited."""
        if self.editing is None or not events:
            return False
        if self.lobby.edit(self.editing, events):
            self.editing = None
        return True
