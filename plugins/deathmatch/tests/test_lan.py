"""Real loopback sockets and two native Fio sessions; no network fakes."""
import json
from pathlib import Path
import socket
import struct
import time

import pytest

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.player import Player
from engine.threaded_game_state import ThreadedGameState
from plugins.manager import get_manager, load_plugins
from plugins.deathmatch.lan import LanHost, LanClient, Discovery, MAX_PACKET
from plugins.deathmatch.network_game import HostGame, ClientGame

pytestmark = pytest.mark.qt


def wait_until(predicate, pump=lambda: None, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("LAN condition timed out")


def test_udp_discovery_and_tcp_disconnect():
    host = LanHost("Loopback Arena", "ctf", address="127.0.0.1", port=0, discovery_port=0)
    client = None
    try:
        scan = Discovery(destinations=("127.0.0.1",), port=host.discovery_port)
        wait_until(scan.done.is_set)
        offer = scan.results.get_nowait()
        assert offer["name"] == "Loopback Arena" and offer["mode"] == "ctf"
        assert offer["port"] == host.port and offer["address"] == "127.0.0.1"
        client = LanClient("127.0.0.1", host.port, "Guest", "blue")
        wait_until(lambda: len(host.peers) == 1)
        client.close()
        wait_until(lambda: not host.peers)
    finally:
        if client: client.close()
        host.close()


def test_oversized_packet_is_disconnected_without_loading_json():
    host = LanHost("Arena", "deathmatch", address="127.0.0.1", port=0, discovery_port=0)
    sock = socket.create_connection(("127.0.0.1", host.port))
    try:
        wait_until(lambda: len(host.peers) == 1)
        sock.sendall(struct.pack("!I", MAX_PACKET + 1))
        wait_until(lambda: not host.peers)
    finally:
        sock.close(); host.close()


@pytest.fixture
def linked(request):
    load_plugins()
    manager = get_manager(); plugin = manager.find_plugin("deathmatch")
    was_enabled = plugin.enabled
    manager.set_enabled(plugin, True)
    filename = "CTF_Arena.json" if getattr(request, "param", "dm") == "ctf" else "Deathmatch_Arena.json"
    data = json.loads((Path(__file__).parents[1] / filename).read_text())
    logics = []
    def build(level):
        state = EditorState(); state.load_from_data(level)
        logic = LogicThread(ThreadedGameState(), state)
        logic.player_runtime.player = Player(0, 0)
        logic.session_runtime.apply_play_mode(True)
        logics.append(logic)
        return logic, plugin._sessions[logic]
    hl, hs = build(data)
    for f in hs.fighters.values():
        f.fire_at = 1000
    transport = LanHost("Test Arena", hs.game.mode, address="127.0.0.1", port=0, discovery_port=0)
    host = HostGame(hs, transport, hs.map_data, "Host")
    connection = LanClient("127.0.0.1", transport.port, "Guest", "blue")
    welcome = []
    def handshake():
        host.tick(1 / 60)
        welcome.extend(p for p in connection.drain() if p.get("type") == "welcome")
    wait_until(lambda: bool(welcome), handshake)
    cl, cs = build(welcome[0]["map"])
    client = ClientGame(cs, connection, welcome[0])
    remote = next(iter(host.peers.values()))
    def pump(ticks=1):
        for _ in range(ticks):
            hl._tick_play_mode(1 / 60)
            cl._tick_play_mode(1 / 60)
            time.sleep(0.002)
    wait_until(lambda: cs.fighters["player"].team == remote.team and len(cs.fighters) == len(hs.fighters), pump)
    yield hl, hs, cl, cs, host, client, remote, pump
    connection.close(); transport.close()
    for logic in reversed(logics):
        logic.session_runtime.apply_play_mode(False); logic.stop()
    manager.set_enabled(plugin, was_enabled)


def test_two_sessions_movement_health_pickups_and_host_authority(linked):
    import glm
    from engine.prop_entity import Prop
    hl, hs, cl, cs, host, client, remote, pump = linked
    remote.actor.pos = glm.vec3(700, 50, 0)
    remote.actor.angle = 0
    cl.player_runtime.player.pos = glm.vec3(700, 50, 0)
    cl.player_runtime.player.angle = 0
    cl.game_state.set_keys({0x57})
    initial = remote.actor.pos.z
    pump(80)
    assert remote.actor.pos.z > initial + 40
    assert "host" in cs.fighters
    cl.game_state.set_keys(set())
    # Client-provided health and position never become authoritative.
    client.transport.send(dict(type="input", keys=[], angle=0, pitch=0, weapon="gun1", hp=9999, pos=[99999]*3))
    pump(10)
    assert hs.health(remote) == 100 and abs(remote.actor.pos.x) < 2000
    prop = Prop(list(remote.actor.pos), {"collect_enabled": True, "collect_type": "health", "collect_value": 50})
    hl.editor_state.things.append(prop); hl.world_runtime.build_entity_caches()
    # Put the same map-authored ID on the client to test pickup state replication.
    mirror = Prop(list(prop.pos), dict(prop.properties))
    cl.editor_state.things.append(mirror); cl.world_runtime.build_entity_caches()
    remote.entity.properties["health"] = 20
    pump(25)
    assert prop.properties["collect_collected"] and hs.health(remote) == 70
    assert mirror.properties["collect_collected"]
    assert cl.player_runtime.player_health == 70
    assert hl.player_runtime.player_health == 100


def test_remote_shooting_and_disconnect_remove_actor(linked):
    import glm
    hl, hs, cl, cs, host, client, remote, pump = linked
    hs.protection = 0
    hs.fighters["player"].protected_until = remote.protected_until = 0
    hl.player_runtime.player.pos = glm.vec3(0, 50, -700)
    remote.actor.pos = glm.vec3(0, 50, -400)
    cl.player_runtime.player.pos = glm.vec3(0, 50, -400)
    cl.player_runtime.player.angle = 3.14159265
    # Normal client path consumes a shot and forwards it to the host.
    cl.combat_runtime.select_slot(2)
    cl.game_state.queue_shot()
    pump(30)
    assert hl.player_runtime.player_health == 25
    key = remote.key
    client.close()
    wait_until(lambda: key not in hs.fighters, lambda: host.tick(1 / 60))
    assert remote.entity not in hl.world_runtime.monster_things


def test_host_pause_fraglimit_and_respawn_are_authoritative(linked):
    hl, hs, cl, cs, host, client, remote, pump = linked
    hl.session_runtime.set_world_paused("test", True)
    before = hs.clock
    pump(20)
    assert hs.clock == before and cl.session_runtime.world_paused
    hl.session_runtime.set_world_paused("test", False)
    pump(15)
    assert not cl.session_runtime.world_paused
    hs.kill(remote, hs.fighters["player"])
    pump(20)
    assert cl.player_runtime.player_dead
    pump(200)
    assert remote.alive and not cl.player_runtime.player_dead
    assert remote.supplies.weapons == {"gun1", "gun2"}
    assert hs.remove_bot(remote.key).startswith("No bot named")
    hs.fighters["player"].frags = 3
    hs.set_frag_limit(3)
    pump(20)
    assert cs.finished and cs.result_winner == "Host"


@pytest.mark.parametrize("linked", ["ctf"], indirect=True)
def test_ctf_flag_carrier_and_capture_replicate(linked):
    import glm
    hl, hs, cl, cs, host, client, remote, pump = linked
    own = hs.game.flags["blue"]
    enemy = hs.game.flags["red"]
    remote.actor.pos = glm.vec3(enemy.home) + glm.vec3(0, 50, 0)
    cl.player_runtime.player.pos = glm.vec3(remote.actor.pos)
    pump(25)
    assert remote.carrying == "red"
    assert cs.fighters["player"].carrying == "red"
    assert cs.game.flags["red"].carrier == "player"
    remote.actor.pos = glm.vec3(own.home) + glm.vec3(0, 50, 0)
    cl.player_runtime.player.pos = glm.vec3(remote.actor.pos)
    pump(25)
    assert hs.game.scores["blue"] == 1 and cs.game.scores["blue"] == 1


def test_lobby_exposes_modes_and_empty_discovery(qt_app, main_window):
    from plugins.deathmatch.lobby import LanLobby
    load_plugins()
    lobby = LanLobby(main_window, get_manager().find_plugin("deathmatch"))
    assert [lobby.mode.itemData(i) for i in range(lobby.mode.count())] == ["deathmatch", "ctf", "teamdeathmatch"]
    lobby.join_selected()
    assert "Select" in lobby.status.text()
    lobby.connect_to("", 27961)
    assert "Enter" in lobby.status.text()
    lobby.close(); lobby.deleteLater(); qt_app.processEvents()


def test_lobby_hosts_real_ctf_session(qt_app, main_window):
    from plugins.deathmatch.lobby import LanLobby
    load_plugins()
    plugin = get_manager().find_plugin("deathmatch")
    lobby = LanLobby(main_window, plugin)
    lobby.mode.setCurrentIndex(1)
    lobby.nickname.setText("LAN Host")
    lobby.host_game()
    logic = main_window.view_3d.logic_thread
    try:
        assert main_window.view_3d.play_mode, lobby.status.text()
        session = plugin._sessions[logic]
        assert session.game.ctf and isinstance(session.network, HostGame)
        assert session.fighters["player"].name == "LAN Host"
        assert session.game.flags["blue"].entity.properties["sprite_path"].endswith("blue_flag.png")
    finally:
        if main_window.view_3d.play_mode:
            main_window.enter_play_mode()
        lobby.close(); lobby.deleteLater(); qt_app.processEvents()


def test_lobby_joins_real_host_and_enters_client_play(linked, qt_app, main_window):
    from plugins.deathmatch.lobby import LanLobby
    hl, hs, cl, cs, host, client, remote, pump = linked
    plugin = get_manager().find_plugin("deathmatch")
    lobby = LanLobby(main_window, plugin)
    lobby.nickname.setText("GUI Guest")
    lobby.connect_to("127.0.0.1", host.transport.port)
    def advance():
        host.tick(1 / 60)
        lobby.pump()
        qt_app.processEvents()
    try:
        wait_until(lambda: main_window.view_3d.play_mode, advance)
        logic = main_window.view_3d.logic_thread
        session = plugin._sessions[logic]
        assert isinstance(session.network, ClientGame)
        assert session.fighters["player"].name == "GUI Guest"
        assert len(host.peers) == 2
    finally:
        if main_window.view_3d.play_mode:
            main_window.enter_play_mode()
        lobby.close(); lobby.deleteLater(); qt_app.processEvents()


def test_remote_heading_and_attack_frames_replicate(linked):
    import math
    from engine.entity_table import EntityTable

    hl, hs, cl, cs, host, client, remote, pump = linked
    server = hs.fighters['player']
    server.actor.angle = math.pi / 2
    server.shoot_until = hs.clock + 1
    hs.publish()
    client.apply(host.snapshot())
    mirror = cs.fighters['host'].entity
    table = EntityTable()
    table.begin_frame([mirror], epoch=1)
    assert table.monster_angle[0] == pytest.approx(90)
    assert mirror.properties['is_shooting']
    server.actor.angle = -math.pi / 2
    server.shoot_until = hs.clock
    client.apply(host.snapshot())
    table.begin_frame([mirror], epoch=1)
    assert table.monster_angle[0] == pytest.approx(270)
    assert not mirror.properties['is_shooting']


@pytest.mark.parametrize('linked', ['dm', 'ctf'], indirect=True)
def test_all_eight_idle_and_attack_views_survive_lan_replication(linked):
    import math
    from pathlib import Path
    import numpy as np
    from engine.entity_table import EntityTable
    from engine.monster_facing import sprite_keys_for_view

    hl, hs, cl, cs, host, client, remote, pump = linked
    root = Path(__file__).resolve().parents[3]
    for shooting in (False, True):
        for fighter in hs.fighters.values():
            fighter.actor.angle = math.pi / 2
            fighter.shoot_until = hs.clock + 1 if shooting else hs.clock
        hs.publish()
        client.apply(host.snapshot())
        visible = [f for f in hs.fighters.values() if f.entity is not None]
        visible += [f for f in cs.fighters.values() if f.entity is not None]
        for fighter in visible:
            table = EntityTable()
            table.begin_frame([fighter.entity], epoch=1)
            for rotation in range(1, 9):
                bearing = fighter.actor.angle + (rotation - 1) * math.pi / 4
                eye = np.asarray(fighter.entity.pos) + [math.sin(bearing) * 400, 0, math.cos(bearing) * 400]
                key = sprite_keys_for_view(table, np.array([0]), eye)[0]
                _, filename, folder, _ = table.sprite_recipes()[key][0]
                assert filename == f'{"shoot" if shooting else "idle"}_{rotation}.png'
                assert (root / 'assets' / folder / filename).is_file()
