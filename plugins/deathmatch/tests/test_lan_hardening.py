"""Hostile and broken peers, on real loopback sockets and native sessions."""
import json
from pathlib import Path
import socket
import struct
import threading
import time

import pytest

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState
from plugins.manager import get_manager, load_plugins
from plugins.deathmatch import lan
from plugins.deathmatch.lan import LanHost, LanClient, PROTOCOL, MAX_PACKET, encode
from plugins.deathmatch.network_game import HostGame, ClientGame

pytestmark = pytest.mark.qt

ARENAS = Path(__file__).parents[1]


def wait_until(predicate, pump=lambda: None, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("LAN condition timed out")


def frame(message):
    data = json.dumps(message).encode()
    return struct.pack("!I", len(data)) + data


class Wire:
    """A raw protocol peer: sends anything, reads frames."""
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.sock.settimeout(0.02)
        self.buffer = bytearray()
        self.closed = False

    def send(self, message):
        try:
            self.sock.sendall(message if isinstance(message, bytes) else frame(message))
            return True
        except OSError:
            self.closed = True
            return False

    def hello(self, name="Guest", **extra):
        return self.send(dict(type="hello", protocol=PROTOCOL, name=name, **extra))

    def read(self):
        """Every complete message received so far."""
        try:
            while True:
                part = self.sock.recv(1 << 16)
                if not part:
                    self.closed = True
                    break
                self.buffer.extend(part)
        except socket.timeout:
            pass
        except OSError:
            self.closed = True
        messages = []
        while len(self.buffer) >= 4:
            size, = struct.unpack("!I", self.buffer[:4])
            if len(self.buffer) < 4 + size:
                break
            messages.append(json.loads(self.buffer[4:4 + size]))
            del self.buffer[:4 + size]
        return messages

    def close(self):
        self.sock.close()


@pytest.fixture
def arena(request):
    """``build(level)`` makes a native Deathmatch session in Play."""
    load_plugins()
    manager = get_manager(); plugin = manager.find_plugin("deathmatch")
    was_enabled = plugin.enabled
    manager.set_enabled(plugin, True)
    logics, cleanup = [], []
    def build(level=None):
        level = level or json.loads((ARENAS / "Deathmatch_Arena.json").read_text())
        state = EditorState(); state.load_from_data(level)
        logic = LogicThread(ThreadedGameState(), state)
        logic.player_runtime.player = Player(0, 0)
        logic.session_runtime.apply_play_mode(True)
        logics.append(logic)
        return logic, plugin._sessions[logic]
    build.cleanup = cleanup
    yield build
    for action in cleanup:
        action()
    for logic in reversed(logics):
        logic.session_runtime.apply_play_mode(False); logic.stop()
    manager.set_enabled(plugin, was_enabled)


@pytest.fixture
def hosted(arena):
    logic, session = arena()
    for fighter in session.fighters.values():
        fighter.fire_at = 1000
    transport = LanHost("Test Arena", session.game.mode, address="127.0.0.1", port=0, discovery_port=0)
    arena.cleanup.append(transport.close)
    host = HostGame(session, transport, session.map_data, "Host")
    wires = []
    def connect():
        wire = Wire(transport.port)
        wires.append(wire)
        return wire
    def tick(count=1):
        for _ in range(count):
            logic._tick_play_mode(1 / 60)
    arena.cleanup.append(lambda: [wire.close() for wire in wires])
    return logic, session, host, connect, tick


def joined(hosted, name="Guest", **extra):
    logic, session, host, connect, tick = hosted
    wire = connect()
    wire.hello(name, **extra)
    before = set(host.peers)
    wait_until(lambda: set(host.peers) - before, tick)
    key = (set(host.peers) - before).pop()
    return wire, key, host.peers[key]


# -- host: every field of every message is hostile -------------------------

HOSTILE = [None, True, False, 0, -1, 2 ** 31, 10 ** 400, -10 ** 400, 1e308, float("nan"),
           float("inf"), "", "x" * 5000, "\x00\n\r\t\x1b[2J", [], [[]], {}, {"a": {"b": []}},
           ["gun1"], "player", "auto", "red"]


def test_host_tick_survives_hostile_field_values(hosted, monkeypatch):
    logic, session, host, connect, tick = hosted
    monkeypatch.setattr(lan, "MAX_MESSAGE_RATE", 10 ** 6)   # Every value in one burst.
    wire, key, fighter = joined(hosted)
    fields = dict(input=("keys", "angle", "pitch", "weapon", "shoot", "use", "glasses"),
                  spectate=("enabled", "target", "glasses"),
                  hello=("protocol", "name", "team", "glasses"))
    for kind, names in fields.items():
        for name in names:
            for value in HOSTILE:
                message = dict(type=kind, protocol=PROTOCOL, keys=[0x57], angle=1.0, pitch=0.1,
                               weapon="gun1", enabled=False)
                message[name] = value
                # json.dumps writes NaN, Infinity and 401-digit integers as
                # Python's own reader accepts them.
                assert wire.send(message)
    for value in HOSTILE:
        wire.send(dict(type="input", keys=[value] * 10))
        wire.send(dict(type=value))
    wire.send(dict(type="spectate", enabled=True, target=fighter.key))
    wire.send(dict(type="spectate", enabled=False))
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        tick()   # A raising tick ends the host's whole Play session.
        time.sleep(0.002)
    assert key in host.peers and not wire.closed
    import math
    assert all(math.isfinite(v) for v in (*fighter.actor.pos, fighter.actor.angle, fighter.actor.pitch))
    assert set(host.inputs[key]["keys"]) <= {0x57, 0x53, 0x41, 0x44, 0x20, 0x43, 0x01000020}


def test_guest_names_are_printable_single_line_and_unique(hosted):
    logic, session, host, connect, tick = hosted
    _, _, first = joined(hosted, "Host")
    _, _, second = joined(hosted, "Evil\n\x1b[31mHost > You\x00‮")
    _, _, third = joined(hosted, ["not", "text"])
    _, _, fourth = joined(hosted, "\n\t\x00")
    names = [f.name for f in session.fighters.values()]
    assert len({name.casefold() for name in names}) == len(names)
    assert first.name == "Host 2"
    for fighter in (first, second, third, fourth):
        assert fighter.name and fighter.name.isprintable() and len(fighter.name) <= 32
        assert fighter.name == fighter.name.strip()
    assert second.name == "Evil[31mHost > You"
    assert (third.name, fourth.name) == ("Player", "Player 2")


def test_messages_before_hello_and_repeated_hello_are_ignored(hosted):
    logic, session, host, connect, tick = hosted
    wire = connect()
    before = len(session.fighters)
    wire.send(dict(type="input", keys=[0x57], angle=1, pitch=0))
    wire.send(dict(type="spectate", enabled=True))
    wire.send(dict(type="hello", protocol="fio-arena-0", name="Old"))
    tick(5); time.sleep(0.1); tick(5)
    assert len(session.fighters) == before and not host.peers
    assert [m["type"] for m in wire.read()] == ["error"]
    wire.hello("One")
    wait_until(lambda: len(host.peers) == 1, tick)
    wire.hello("Two"); wire.hello("Three")
    tick(5); time.sleep(0.1); tick(5)
    assert len(session.fighters) == before + 1
    assert next(iter(host.peers.values())).name == "One"


# -- host: liveness and leaks ------------------------------------------------

def test_flood_from_one_peer_cannot_leak_another_peers_fighter(hosted):
    logic, session, host, connect, tick = hosted
    before = set(session.fighters)
    victim, victim_key, _ = joined(hosted, "Victim")
    flooder, flooder_key, _ = joined(hosted, "Flooder")
    burst = frame(dict(type="input", keys=[], angle=0, pitch=0)) * 400
    stop = threading.Event()
    def flood():
        while not stop.is_set() and flooder.send(burst):
            pass
    thread = threading.Thread(target=flood, daemon=True); thread.start()
    try:
        # The host hitches (a map load, a debugger) with the event queue full,
        # and meanwhile an honest player leaves.
        wait_until(lambda: host.transport.events.full() or flooder_key not in host.transport.peers)
        victim.close()
        wait_until(lambda: victim_key not in host.transport.peers)
        time.sleep(0.2)
    finally:
        stop.set(); thread.join(2)
    for _ in range(30):
        tick(); time.sleep(0.005)
    assert victim_key not in host.peers
    assert "Victim" not in [f.name for f in session.fighters.values()]
    # The flooder is far beyond any real client's 20 messages a second.
    assert flooder_key not in host.peers
    assert set(session.fighters) == before


def test_silent_connections_cannot_hold_every_player_slot(hosted, monkeypatch):
    logic, session, host, connect, tick = hosted
    monkeypatch.setattr(HostGame, "HELLO_TIMEOUT", 0.3, raising=False)
    squatters = [connect() for _ in range(host.transport.capacity - 1)]
    wait_until(lambda: len(host.transport.peers) == host.transport.capacity - 1)
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        for wire in squatters:
            wire.send(dict(type="noise"))    # Defeats the idle-read deadline.
        tick(); time.sleep(0.01)
    assert not host.transport.peers
    wire, key, fighter = joined(hosted, "Honest")
    assert fighter.name == "Honest"


def test_connect_disconnect_churn_returns_to_baseline(hosted):
    logic, session, host, connect, tick = hosted
    tick(3)
    fighters, things = set(session.fighters), len(logic.editor_state.things)
    spawned, threads = len(session._spawned), threading.active_count()
    for round_ in range(40):
        wires = [connect() for _ in range(3)]
        for index, wire in enumerate(wires):
            wire.hello(f"Churn {round_}")
        if round_ % 2:
            wait_until(lambda: len(host.peers) == 3, tick)
        for wire in wires:
            wire.close()
        wait_until(lambda: not host.transport.peers and not host.peers, tick)
    wait_until(lambda: threading.active_count() <= threads, tick)
    assert set(session.fighters) == fighters
    assert len(logic.editor_state.things) == things
    assert len(session._spawned) == spawned
    assert not host.inputs


def test_host_refuses_a_map_no_guest_could_receive(arena):
    logic, session = arena()
    transport = LanHost("Big", "deathmatch", address="127.0.0.1", port=0, discovery_port=0)
    arena.cleanup.append(transport.close)
    big = dict(session.map_data, padding="x" * MAX_PACKET)
    with pytest.raises(ValueError, match="too large"):
        HostGame(session, transport, big, "Host")
    assert session.network is None
    HostGame(session, transport, session.map_data, "Host")


def test_slow_link_still_receives_a_large_welcome(arena):
    logic, session = arena()
    transport = LanHost("Slow", "deathmatch", address="127.0.0.1", port=0, discovery_port=0)
    arena.cleanup.append(transport.close)
    # 900 KiB: a real "Current map" (maps/BigWorld_streaming_test.json is larger).
    host = HostGame(session, transport, dict(session.map_data, padding="x" * (900 * 1024)), "Host")
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
    sock.connect(("127.0.0.1", transport.port))
    arena.cleanup.append(sock.close)
    wait_until(lambda: len(transport.peers) == 1)
    next(iter(transport.peers.values())).sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    sock.sendall(frame(dict(type="hello", protocol=PROTOCOL, name="Slow")))
    wait_until(lambda: host.peers, lambda: host.tick(1 / 60))
    # About 3 Mbit/s: slower than the old one-second budget for a whole frame.
    received, sock_closed = bytearray(), False
    sock.settimeout(2)
    end = time.monotonic() + 8
    while time.monotonic() < end and len(received) < 900 * 1024:
        part = sock.recv(32768)
        if not part:
            sock_closed = True
            break
        received.extend(part)
        time.sleep(0.1)
    assert not sock_closed and len(received) >= 900 * 1024


def test_host_can_stop_and_host_again_on_the_same_port_with_guests_connected(arena):
    logic, session = arena()
    first = LanHost("One", "deathmatch", address="127.0.0.1", port=0, discovery_port=0)
    port, discovery = first.port, first.discovery_port
    guests = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
    wait_until(lambda: len(first.peers) == 3)
    threads = threading.active_count()
    first.close()
    second = LanHost("Two", "deathmatch", address="127.0.0.1", port=port, discovery_port=discovery)
    try:
        assert second.port == port
        wait_until(lambda: threading.active_count() <= threads - 6)
        for guest in guests:
            guest.settimeout(2)
            assert guest.recv(1) == b""
    finally:
        second.close()
        for guest in guests:
            guest.close()


def test_connection_accepted_while_the_host_closes_is_not_leaked():
    host = LanHost("Race", "deathmatch", address="127.0.0.1", port=0, discovery_port=0)
    listener = host.listener

    class Racing:
        """accept() returns a guest just as the host stops."""
        def accept(self):
            result = listener.accept()
            closer = threading.Thread(target=host.close, daemon=True)
            closer.start(); closer.join(0.3)
            return result
        def __getattr__(self, name):
            return getattr(listener, name)

    host.listener = Racing()
    time.sleep(0.3)      # The accept loop picks the wrapper up on its next pass.
    guest = socket.create_connection(("127.0.0.1", host.port))
    try:
        wait_until(host.closed.is_set)
        guest.settimeout(3)
        assert guest.recv(1) == b""       # The host hung up; no orphan channel.
        wait_until(lambda: not host.peers)
    finally:
        host.close(); guest.close()


# -- client: the host is hostile too -------------------------------------------

class FakeHost:
    """A listening socket that says whatever a test tells it to."""
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0)); self.listener.listen(4)
        self.listener.settimeout(3)
        self.port = self.listener.getsockname()[1]
        self.peer = None

    def accept(self):
        self.peer, _ = self.listener.accept()
        self.peer.settimeout(3)
        return self.peer

    def close(self):
        self.listener.close()
        if self.peer is not None:
            self.peer.close()


class Feed:
    """ClientGame's transport, fed by the test instead of a socket."""
    def __init__(self):
        self.packets, self.sent, self.closed = [], [], False

    def drain(self):
        packets, self.packets = self.packets, []
        return iter(packets)

    def send(self, message):
        self.sent.append(message)
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def client(arena):
    """A native client session following a scripted host, plus a real snapshot."""
    host_logic, host_session = arena()
    host = HostGame(host_session, Feed(), host_session.map_data, "Host")
    host.transport.connections = lambda: {}
    host_session.add_bot("Guest", "normal", "auto")
    you = next(f for f in host_session.fighters.values() if f.name == "Guest")
    you.role = "remote"
    logic, session = arena(json.loads(json.dumps(host_session.map_data)))
    feed = Feed()
    game = ClientGame(session, feed, dict(type="welcome", protocol=PROTOCOL, you=you.key, name="Guest"))
    snapshot = lambda: json.loads(json.dumps(host.snapshot()))
    feed.packets.append(snapshot())
    logic._tick_play_mode(1 / 60)
    assert len(session.fighters) == len(host_session.fighters) and not session.finished
    return logic, session, game, feed, snapshot


def world_is_sane(logic, session):
    import math
    rt, combat = logic.player_runtime, logic.combat_runtime
    numbers = [*rt.player.pos, rt.player.angle, rt.player.pitch, rt.player_health, rt.player_armor,
               combat.player_ammo, session.clock]
    for fighter in session.fighters.values():
        numbers += [*fighter.actor.pos, fighter.actor.angle, fighter.actor.pitch, fighter.frags,
                    fighter.deaths, fighter.respawn_at, fighter.protected_until, fighter.shoot_until]
        assert isinstance(fighter.name, str) and isinstance(fighter.key, str)
    assert all(type(v) in (int, float) and math.isfinite(v) and abs(v) < 1e13 for v in numbers), numbers
    assert all(isinstance(text, str) for _, text in session.feed) and len(session.feed) <= 4
    state = session.snapshot
    assert dict(state["team_scores"]).keys() == {"red", "blue"}


def test_client_tick_survives_hostile_snapshot_fields(client):
    logic, session, game, feed, snapshot = client
    refused = applied = 0

    def deliver(packet):
        nonlocal refused, applied
        game.disconnected, session.finished = False, False
        feed.packets.append(packet)
        logic._tick_play_mode(1 / 60)   # A raising tick ends the whole Play session.
        logic._tick_play_mode(1 / 60)
        logic.render_runtime.prepare_render_state()
        world_is_sane(logic, session)
        if game.disconnected:
            refused += 1
            assert session.reason == "Invalid host snapshot"
        else:
            applied += 1

    base = snapshot()
    for name in base:
        for value in HOSTILE:
            deliver(dict(base, **{name: value}))
        packet = dict(base); del packet[name]
        deliver(packet)
    for group, limit in (("actors", 3), ("flags", 2)):
        for index in range(min(limit, len(base[group]))):
            for name in base[group][index]:
                for value in HOSTILE:
                    packet = json.loads(json.dumps(base))
                    packet[group][index][name] = value
                    deliver(packet)
                packet = json.loads(json.dumps(base))
                del packet[group][index][name]
                deliver(packet)
    deliver(dict(base, actors=base["actors"] * 100))
    deliver(dict(base, actors=[dict(base["actors"][0], key=f"ghost{i}") for i in range(128)]))
    deliver(dict(base, flags=[dict(team="red", pos=[], carrier=None, dropped=False)]))
    deliver(dict(base, pickups=[["x", True]] * 30000))
    deliver(dict(base, pickups=[[["unhashable"], True]]))
    deliver(dict(base, feed=["line"] * 60))
    deliver(dict(base, feed=["line"] * 100000))
    deliver(dict(base, scores=dict(red="many", blue=[])))
    deliver(dict(base, scores={}))
    assert refused > 100 and applied > 20
    # And an honest host is followed again afterwards.
    game.disconnected, session.finished = False, False
    feed.packets.append(base)
    logic._tick_play_mode(1 / 60)
    assert not game.disconnected and len(session.fighters) == len(base["actors"])
    assert len(session.feed) <= 4


def test_actor_churn_does_not_grow_the_client(client):
    logic, session, game, feed, snapshot = client
    base = snapshot()
    things, spawned = len(logic.editor_state.things), len(session._spawned)
    for round_ in range(30):
        extra = [dict(base["actors"][0], key=f"g{round_}-{i}", name=f"G{i}") for i in range(20)]
        feed.packets += [dict(base, actors=base["actors"] + extra), base]
        logic._tick_play_mode(1 / 60)
    assert len(logic.editor_state.things) == things
    assert len(session._spawned) == spawned
    assert len(session.fighters) == len(base["actors"])


@pytest.mark.parametrize("welcome", [
    {}, dict(you=None), dict(you=["player"]), dict(you={"a": 1}), dict(you=""), dict(you=7),
    dict(you="x" * 5000), dict(you="abc", protocol="fio-arena-0")])
def test_client_refuses_a_malformed_welcome_before_touching_the_session(arena, welcome):
    logic, session = arena()
    fighters = set(session.fighters)
    with pytest.raises(ValueError):
        ClientGame(session, Feed(), dict(type="welcome", **welcome))
    assert session.network is None and set(session.fighters) == fighters


def test_client_name_from_welcome_is_one_printable_line(arena):
    logic, session = arena()
    for name, expected in ((["a"], "You"), ("A\nB\x00", "AB"), (None, "You"), ("x" * 500, "x" * 32)):
        ClientGame(session, Feed(), dict(type="welcome", protocol=PROTOCOL, you="abc", name=name))
        assert session.fighters["player"].name == expected
        session.publish()


def test_client_learns_of_a_disconnect_when_its_event_queue_was_full(arena):
    fake = FakeHost()
    arena.cleanup.append(fake.close)
    connection = LanClient("127.0.0.1", fake.port, "Guest")
    arena.cleanup.append(connection.close)
    peer = fake.accept()
    # The client is busy loading the map (or sits in a dialog) and drains
    # nothing while the host keeps sending snapshots.
    for index in range(400):
        peer.sendall(frame(dict(type="snapshot", clock=index)))
    wait_until(lambda: connection.channel is not None and connection.channel.closed.is_set())
    events = list(connection.drain())
    assert len(events) > 100
    assert events[-1] == {"type": "disconnected"}
    assert [e for e in events if e.get("type") == "disconnected"] == [events[-1]]
    assert list(connection.drain()) == []
    logic, session = arena()
    game = ClientGame(session, connection, dict(protocol=PROTOCOL, you="abc", name="Guest"))
    connection.lost.set(); connection._lost_reported = False
    logic._tick_play_mode(1 / 60)
    assert game.disconnected and session.finished and session.reason == "Host disconnected"


def test_disconnect_straight_after_the_welcome_reaches_the_game():
    fake = FakeHost()
    connection = LanClient("127.0.0.1", fake.port, "Guest")
    try:
        peer = fake.accept()
        peer.sendall(frame(dict(type="welcome", protocol=PROTOCOL, you="abc", name="Guest", map={})))
        peer.close()
        wait_until(lambda: connection.lost.is_set())
        # Both lobbies stop at the welcome and hand the connection to the game.
        for packet in connection.drain():
            if packet.get("type") == "welcome":
                break
        assert [p["type"] for p in connection.drain()] == ["disconnected"]
    finally:
        connection.close(); fake.close()


@pytest.mark.parametrize("address", ["a..b", "x" * 70 + ".example"])
def test_unresolvable_address_is_reported_not_left_connecting(address):
    connection = LanClient(address, 27961, "Guest")
    try:
        events = []
        wait_until(lambda: events.extend(connection.drain()) or events, timeout=8)
        assert events[0]["type"] == "error" and events[0]["message"]
    finally:
        connection.close()


def test_client_closed_while_connecting_leaves_no_connection(monkeypatch):
    fake = FakeHost()
    holder = []

    class Racing(lan.Channel):
        def __init__(self, *args, **kwargs):
            holder[0].close()        # close() lands while the channel is being made.
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(lan, "Channel", Racing)
    holder.append(LanClient.__new__(LanClient))
    try:
        holder[0].__init__("127.0.0.1", fake.port, "Guest")
        peer = fake.accept()
        data = b"x"
        while data:                   # At most the hello, then end of stream.
            data = peer.recv(4096)
    finally:
        holder[0].close(); fake.close()


# -- discovery -----------------------------------------------------------------

def test_discovery_flood_is_capped_and_ports_are_real_integers():
    responder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    responder.bind(("127.0.0.1", 0)); responder.settimeout(3)
    stop = threading.Event()
    def reply():
        try:
            _, address = responder.recvfrom(2048)
        except OSError:
            return
        offer = dict(protocol=PROTOCOL, type="offer", name="Flood", mode="deathmatch", players=1, capacity=8)
        responder.sendto(json.dumps(dict(offer, port=True)).encode(), address)
        responder.sendto(json.dumps(dict(offer, port=1.5)).encode(), address)
        for port in range(1, 3000):
            if stop.is_set():
                break
            try:
                responder.sendto(json.dumps(dict(offer, port=port)).encode(), address)
            except OSError:
                break
    thread = threading.Thread(target=reply, daemon=True); thread.start()
    try:
        scan = lan.Discovery(destinations=("127.0.0.1",), port=responder.getsockname()[1])
        wait_until(scan.done.is_set)
        offers = []
        while not scan.results.empty():
            offers.append(scan.results.get_nowait())
        assert 0 < len(offers) <= lan.MAX_OFFERS
        assert all(type(offer["port"]) is int for offer in offers)
    finally:
        stop.set(); thread.join(3); responder.close()
