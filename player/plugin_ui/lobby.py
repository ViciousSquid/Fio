"""Deathmatch LAN lobby for the player: discover, host and join.

A Qt-free port of the plugin's desktop ``LanLobby`` dialog. Discovery,
transport and the game protocol are the plugin's own (``lan.py`` and
``network_game.py``), so desktop and Android peers find and join each other.

The player only plays worlds exported from desktop Fio. Hosting therefore
uses the world that is open, with the lobby's game settings applied the way
desktop applies them. Joining loads the map the host sends and draws it with
the assets of the world open on this device, so open the same exported
world before joining.
"""
from __future__ import annotations

import queue
import socket

from . import arena

#: Text fields the lobby lets the player edit, with their length limits.
FIELDS = {'name': 32, 'address': 64, 'game_name': 48}
FRAG_STEPS = (0, 5, 10, 15, 20, 25, 30, 50, 75, 100)
CAPTURE_STEPS = (0, 1, 2, 3, 5, 7, 10, 15, 20)


def _lan():
    from plugins.deathmatch import lan
    return lan


def local_address():
    """This device's LAN address, for hosts to tell joining players."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent: connecting a UDP socket only selects a route.
        probe.connect(('10.255.255.255', 1))
        address = probe.getsockname()[0]
    except OSError:
        address = '127.0.0.1'
    finally:
        probe.close()
    return address


def broadcast_destinations(address=None, netmask=None):
    """Where discovery queries go: limited, subnet-directed and loopback.

    Android often refuses the limited broadcast address, so the query also
    goes to the Wi-Fi subnet's directed broadcast. Without a known netmask a
    /24 subnet is assumed, as on most home and office LANs.
    """
    destinations = ['255.255.255.255']
    address = address or local_address()
    try:
        octets = [int(part) for part in address.split('.')]
        mask = [int(part) for part in (netmask or '255.255.255.0').split('.')]
        if len(octets) == 4 and len(mask) == 4 and octets[0] not in (0, 127):
            directed = '.'.join(str((o & m) | (~m & 255)) for o, m in zip(octets, mask))
            destinations.append(directed)
    except ValueError:
        pass
    destinations.append('127.0.0.1')
    return tuple(dict.fromkeys(destinations))


def valid_address(text):
    text = text.strip()
    if not text or len(text) > 64 or any(ch.isspace() for ch in text):
        return False
    return all(ch.isalnum() or ch in '.-:' for ch in text)


class Lobby:
    """Lobby state, driven from the pause menu and pumped every frame."""

    def __init__(self):
        self.name = 'Player'
        self.team = 'auto'
        self.address = ''
        self.game_name = 'Fio LAN Arena'
        self.mode = 'deathmatch'
        self.frag_limit = 20
        self.capture_limit = 3
        self.offers = {}
        self.scan = None
        self.pending = None
        self.status = ''
        self.port = None
        self.discovery_port = None

    # -- settings ------------------------------------------------------
    def cycle(self, setting, step=1):
        if setting == 'team':
            options = arena.TEAMS
        elif setting == 'mode':
            options = tuple(value for value, _label in arena.GAME_TYPES)
        elif setting == 'frag_limit':
            options = FRAG_STEPS
        elif setting == 'capture_limit':
            options = CAPTURE_STEPS
        else:
            raise ValueError(setting)
        current = getattr(self, setting)
        index = options.index(current) if current in options else 0
        setattr(self, setting, options[(index + step) % len(options)])

    def mode_label(self):
        return dict(arena.GAME_TYPES).get(self.mode, self.mode)

    def edit(self, field, events):
        """Apply soft-keyboard events to *field*; True when Enter was pressed."""
        limit = FIELDS[field]
        value = getattr(self, field)
        done = False
        for event in events:
            if event[0] == 'text':
                value += ''.join(ch for ch in event[1] if ch.isprintable())
            elif event[0] == 'backspace':
                value = value[:-1]
            elif event[0] == 'enter':
                done = True
        setattr(self, field, value[:limit])
        return done

    # -- discovery -----------------------------------------------------
    def refresh(self):
        if self.scan is not None and not self.scan.done.is_set():
            return
        self.offers.clear()
        self.scan = _lan().Discovery(destinations=broadcast_destinations())
        self.status = 'Searching the LAN…'

    def games(self):
        """Discovered games, sorted: ``[((address, port), offer)]``."""
        return sorted(self.offers.items())

    # -- joining -------------------------------------------------------
    def join(self, address, port=None):
        lan = _lan()
        if not valid_address(address):
            self.status = "Enter the host's LAN address."
            return False
        try:
            port = int(port or lan.GAME_PORT)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            self.status = 'That LAN game has an invalid port.'
            return False
        if self.pending is not None:
            self.pending.close()
        self.pending = lan.LanClient(address.strip(), port,
                                     self.name.strip() or 'Player', self.team)
        self.status = f'Connecting to {address}…'
        return True

    def cancel(self):
        if self.pending is not None:
            self.pending.close()
            self.pending = None
        self.status = ''

    def pump(self):
        """Advance discovery and a pending join; return a welcome, if any.

        Returns ``(connection, welcome packet)`` once a host accepts us; the
        caller loads the map and attaches the plugin's ``ClientGame``.
        """
        if self.scan is not None:
            while True:
                try:
                    offer = self.scan.results.get_nowait()
                except queue.Empty:
                    break
                self.offers[(offer['address'], offer['port'])] = offer
            if self.scan.done.is_set():
                if self.pending is None:
                    self.status = (f'Found {len(self.offers)} LAN game(s).' if self.offers else
                                   'No games found. Host one, or join a LAN address directly.')
                self.scan = None
        if self.pending is None:
            return None
        connection = self.pending
        # Lazily: whatever follows the welcome (a disconnect, say) stays
        # queued for the game that takes the connection over.
        for packet in connection.drain():
            if packet.get('type') == 'welcome':
                self.pending = None
                return connection, packet
            if packet.get('type') in ('error', 'disconnected'):
                self.status = 'Could not join: ' + str(packet.get('message', 'host disconnected'))
                connection.close()
                self.pending = None
                break
        return None

    @staticmethod
    def check_welcome(packet):
        """The host map from a welcome, or raise ValueError."""
        if packet.get('protocol') != _lan().PROTOCOL or not isinstance(packet.get('map'), dict):
            raise ValueError('Incompatible arena handshake')
        from plugins.deathmatch.network_game import handshake
        handshake(packet)
        data = packet['map']
        if not isinstance(data.get('things'), list) or not isinstance(data.get('brushes', []), list):
            raise ValueError('The host sent an invalid map')
        return data

    # -- hosting -------------------------------------------------------
    def prepare_host(self, level):
        """The open world's map with this lobby's game settings applied.

        Mirrors desktop ``LanLobby.host_game``: the map needs Match Settings
        and a PlayerStart, CTF needs exactly one red and one blue flag, and
        the lobby's type, limits and team replace the authored ones.
        """
        data = level
        things = data.get('things', [])
        settings = next((t for t in things if t.get('type') == 'deathmatchsettings'), None)
        if settings is None or not any(t.get('type') == 'playerstart' for t in things):
            raise ValueError('This world needs Match Settings and a PlayerStart to host.')
        if self.mode == 'ctf':
            teams = sorted(t.get('properties', {}).get('team') or '' for t in things
                           if t.get('type') == 'deathmatchflag')
            if teams != ['blue', 'red']:
                raise ValueError('CTF needs exactly one red and one blue CTF Flag.')
        settings.setdefault('properties', {}).update(
            game_type=self.mode, frag_limit=self.frag_limit, capture_limit=self.capture_limit,
            player_team='blue' if self.team == 'blue' else 'red')
        from plugins.deathmatch.network_game import check_map
        check_map(data)
        return data

    def open_host(self):
        """Open the plugin's LAN host transport (TCP game + UDP discovery)."""
        transport = _lan().LanHost(self.game_name.strip() or 'Fio LAN Arena', self.mode)
        self.port, self.discovery_port = transport.port, transport.discovery_port
        return transport

    def close(self):
        self.cancel()
        self.scan = None
