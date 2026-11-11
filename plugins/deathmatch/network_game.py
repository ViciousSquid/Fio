# SPDX-License-Identifier: GPL-2.0-or-later
"""Host-authoritative LAN actors and snapshots, pumped on Fio's logic thread."""
import math
from engine.glasses import normalize_glasses
from engine.level_validation import finite_number

from .lan import PROTOCOL, encode
from .entities import DeathmatchBot
from .supplies import BotSupplies


def finite(value, fallback=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else fallback
    except (ValueError, TypeError, OverflowError):  # float(10 ** 400) overflows.
        return fallback


def clean_name(value, fallback="Player", limit=32):
    """A peer's name as one printable line: it reaches the HUD, feed and console."""
    if not isinstance(value, str):
        return fallback
    return "".join(ch for ch in value[:4 * limit] if ch.isprintable())[:limit].strip() or fallback


def handshake(welcome):
    """``(own actor key, own name)`` from a host's welcome, or raise ValueError."""
    if not isinstance(welcome, dict) or welcome.get("protocol", PROTOCOL) != PROTOCOL:
        raise ValueError("Incompatible arena handshake")
    you = welcome.get("you")
    if not isinstance(you, str) or not 0 < len(you) <= 128:
        raise ValueError("Incompatible arena handshake")
    return you, clean_name(welcome.get("name", "You"), "You")


def check_map(map_data):
    """Raise ValueError unless a welcome carrying *map_data* fits one frame.

    Otherwise every join fails with nothing but a dropped connection, and the
    host is never told why.
    """
    try:
        encode(dict(type="welcome", protocol=PROTOCOL, you="x" * 128, name="x" * 32, map=map_data))
    except (ValueError, TypeError) as error:
        raise ValueError(f"This map is too large or cannot be sent to LAN players ({error}).") from None


class _Bad(ValueError):
    pass


def _number(value, low=-1e12, high=1e12):
    # Comparisons, not float(): a 400-digit JSON integer is a valid int.
    if not finite_number(value) or not low <= value <= high:
        raise _Bad("number")
    return value


def _whole(value, low=-10 ** 9, high=10 ** 9):
    if type(value) is not int or not low <= value <= high:
        raise _Bad("integer")
    return value


def _flag(value):
    if type(value) is not bool:
        raise _Bad("boolean")
    return value


def _text(value, limit=64):
    if not isinstance(value, str):
        raise _Bad("text")
    return "".join(ch for ch in value[:4 * limit] if ch.isprintable())[:limit]


def _key(value):
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        raise _Bad("key")
    return value


def _vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _Bad("vector")
    return [float(_number(v, -1e7, 1e7)) for v in value]


def _items(value, limit):
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise _Bad("list")
    return value


TEAM_NAMES = ("red", "blue")
WEAPON_IDS = ("gun1", "gun2", "custom1", "custom2")
ROLES = ("bot", "remote", "player")


def clean_snapshot(packet):
    """A host snapshot with every field checked, or ``None`` to ignore it.

    Raises ValueError for anything a real host never sends. The world is only
    touched with the result, so a refused snapshot leaves it exactly as it was.
    """
    try:
        records = packet.get("actors", [])
        if not isinstance(records, list) or len(records) > 128:
            return None
        actors = []
        for record in records:
            if not isinstance(record, dict):
                raise _Bad("actor")
            target = record.get("spectator_target")
            carrying = record.get("carrying")
            weapon = record.get("weapon")
            actors.append(dict(
                key=_key(record["key"]), name=_text(record["name"], 48) or "Player",
                role=record.get("role") if record.get("role") in ROLES else "bot",
                team=record.get("team") if record.get("team") in TEAM_NAMES else "",
                pos=_vector(record["pos"]),
                angle=_number(record.get("angle", 0.0), -1e6, 1e6),
                pitch=_number(record.get("pitch", 0.0), -1e6, 1e6),
                ducked=_flag(record.get("ducked", False)),
                spectator=_flag(record.get("spectator", False)),
                spectator_target=_key(target) if target else None,
                glasses=normalize_glasses(record.get("glasses") if isinstance(record.get("glasses"), str) else None),
                hp=_number(record["hp"], -1e9, 1e9), alive=_flag(record["alive"]),
                frags=_whole(record["frags"]), deaths=_whole(record["deaths"]),
                carrying=carrying if carrying in TEAM_NAMES else None,
                captures=_whole(record["captures"]),
                respawn=_number(record["respawn"]), protection=_number(record["protection"]),
                shoot_until=_number(record.get("shoot_until", 0.0)),
                weapons=[w for w in _items(record.get("weapons", ()), 16) if w in WEAPON_IDS],
                ammo=_number(record.get("ammo", 0), -1e9, 1e9), armor=_number(record.get("armor", 0), -1e9, 1e9),
                weapon=weapon if weapon in WEAPON_IDS else None))
        flags = []
        for info in _items(packet.get("flags", ()), 8):
            if not isinstance(info, dict):
                raise _Bad("flag")
            if info.get("team") in TEAM_NAMES:
                flags.append(dict(team=info["team"], pos=tuple(_vector(info["pos"])),
                                  carrier=_key(info["carrier"]) if info.get("carrier") else None,
                                  dropped=_flag(info["dropped"])))
        pickups = []
        for entry in _items(packet.get("pickups", ()), 32768):
            if not isinstance(entry, (list, tuple)) or len(entry) != 2 or type(entry[0]) not in (str, int):
                raise _Bad("pickup")
            pickups.append((entry[0], bool(entry[1])))
        scores = packet["scores"]
        if not isinstance(scores, dict):
            raise _Bad("scores")
        winner = packet.get("winner")
        return dict(
            actors=actors, flags=flags, pickups=pickups,
            clock=_number(packet["clock"], 0, 1e9), finished=_flag(packet["finished"]),
            reason=_text(packet.get("reason") or "", 80),
            winner=None if winner is None else _text(winner, 48),
            scores={team: _whole(scores.get(team, 0)) for team in TEAM_NAMES},
            limit=_whole(packet["limit"], 0), captures=_whole(packet["captures"], 0),
            paused=_flag(packet.get("paused", False)),
            # The host keeps four lines; the HUD draws every line it is given.
            feed=[_text(text, 120) for text in _items(packet.get("feed", ()), 64)[-4:]])
    except (KeyError, TypeError, AttributeError, OverflowError) as error:
        raise ValueError(f"Invalid host snapshot ({type(error).__name__})") from None


class HostGame:
    is_client = False
    #: Seconds a connection may hold a player slot without a valid hello.
    HELLO_TIMEOUT = 10

    def __init__(self, session, transport, map_data, name="Host"):
        check_map(map_data)
        self.session, self.transport, self.map_data = session, transport, map_data
        self.peers = {}
        self.inputs = {}
        self.elapsed = 0
        session.fighters["player"].name = name
        session.network = self

    def _leave(self, key):
        s = self.session
        fighter = self.peers.pop(key, None)
        self.inputs.pop(key, None)
        if fighter is not None:
            s.remove_bot(fighter.key, allow_remote=True)
            if fighter.entity in s._spawned:
                s._spawned.remove(fighter.entity)   # One per join, for as long as Play lasts.

    def tick(self, dt):
        s = self.session
        for event in self.transport.drain():
            kind, key, *data = event
            if kind == "disconnected":
                self._leave(key)
            elif kind == "message":
                packet = data[0]
                if packet.get("type") == "hello" and key not in self.peers:
                    if packet.get("protocol") != PROTOCOL:
                        self.transport.send(key, dict(type="error", message="Incompatible Fio arena protocol."))
                        continue
                    name = clean_name(packet.get("name", "Player"))
                    existing = {f.name.casefold() for f in s.fighters.values()}
                    base, index = name, 2
                    while name.casefold() in existing:
                        name = f"{base[:26]} {index}"; index += 1
                    team = packet.get("team", "auto")
                    team = team if team in ("red", "blue", "auto") else "auto"
                    s.add_bot(name, "normal", team)
                    fighter = next(f for f in s.fighters.values() if f.name == name)
                    fighter.role, fighter.peer = "remote", key
                    fighter.glasses_style = normalize_glasses(packet.get('glasses'))
                    fighter.fire_at = s.clock
                    fighter.supplies.weapons = {"gun1", "gun2"}
                    fighter.supplies.ammo = 24
                    fighter.supplies.active = "gun1"
                    self.peers[key] = fighter
                    self.inputs[key] = dict(keys=[], angle=fighter.actor.angle, pitch=0,
                                            weapon="gun1", at=s.clock)
                    self.transport.send(key, dict(type="welcome", protocol=PROTOCOL, you=fighter.key,
                                                  name=name, map=self.map_data))
                elif packet.get('type') == 'spectate' and key in self.peers:
                    if type(packet.get('enabled')) is not bool:
                        continue
                    target = packet.get('target')
                    target = target if isinstance(target, str) else None
                    other = s.fighters.get(target)
                    fighter = self.peers[key]
                    fighter.glasses_style = normalize_glasses(packet.get('glasses', fighter.glasses_style))
                    if other is None or other is fighter or other.spectator or not other.alive:
                        target = None
                    s.spectators.set(fighter, packet['enabled'], target)
                elif packet.get("type") == "input" and key in self.peers:
                    keys = packet.get("keys", [])
                    if not isinstance(keys, list) or len(keys) > 10:
                        continue
                    allowed = (0x57, 0x53, 0x41, 0x44, 0x20, 0x43, 0x01000020)
                    old = self.inputs[key]
                    fighter = self.peers[key]
                    fighter.glasses_style = normalize_glasses(packet.get('glasses', fighter.glasses_style))
                    self.inputs[key] = dict(keys=[k for k in keys if type(k) is int and k in allowed],
                        angle=finite(packet.get("angle")) % (2 * math.pi),
                        pitch=max(-1.5, min(1.5, finite(packet.get("pitch")))),
                        weapon=packet.get("weapon") if packet.get("weapon") in ("gun1", "gun2", "custom1", "custom2") else "gun1",
                        shoot=(old.get("shoot", False) or packet.get("shoot") is True),
                        use=packet.get("use") is True, at=s.clock)
        # The bounded event queue drops events when it is full, so membership
        # is reconciled with the transport itself, never trusted to events.
        live = self.transport.connections()
        for key in [key for key in self.peers if key not in live]:
            self._leave(key)
        for key, age in live.items():
            if key not in self.peers and age > self.HELLO_TIMEOUT:
                self.transport.drop(key)
        self.elapsed += dt
        if self.elapsed >= 0.1:
            self.elapsed = 0
            packet = self.snapshot()
            for key in list(self.peers):
                self.transport.send(key, packet)

    def update_remote(self, fighter, dt):
        import glm
        from engine.physics import world_gravity
        s = self.session
        data = self.inputs.get(fighter.peer, {})
        keys = set(data.get("keys", [])) if s.clock - data.get("at", -100) < 0.5 else set()
        fighter.actor.angle = data.get("angle", fighter.actor.angle)
        fighter.actor.pitch = data.get("pitch", 0)
        if fighter.spectator:
            if not s.spectators.follow(fighter):
                s.spectators.fly(fighter.actor, dt, keys)
            s.sync_entity_position(fighter)
            data['shoot'] = False
            return
        move = glm.vec3(int(0x41 in keys) - int(0x44 in keys), 0,
                        int(0x57 in keys) - int(0x53 in keys))
        fighter.actor.update(dt, move, 0x20 in keys, 0x43 in keys,
            s.logic.collision_runtime._collision_brushes_cache,
            s.logic.mover_runtime._mover_brush_list, s.logic.mover_runtime._door_brush_list,
            s.logic.world_runtime.terrain, spatial_grid=s.logic.session_runtime.spatial_grid,
            sprint=0x01000020 in keys, gravity=world_gravity(s.logic.session_runtime.physics_world))
        s.sync_entity_position(fighter)
        selected = data.get("weapon", "gun1")
        if selected in fighter.supplies.weapons:
            fighter.supplies.active = selected
        fighter.supplies.collect_nearby(use_allowed=data.get("use", False))
        if data.get("shoot") and s.clock >= fighter.fire_at:
            s.fire_bot(fighter, selected=fighter.supplies.active, cadence=0.05)
        data["shoot"] = False
        if fighter.actor.pos.y < -4096:
            s.kill(fighter)

    def snapshot(self):
        s = self.session
        actors = []
        for f in s.fighters.values():
            supply = f.supplies
            actors.append(dict(key=f.key, name=f.name, role=f.role, team=f.team,
                pos=list(f.actor.pos), angle=f.actor.angle, pitch=f.actor.pitch,
                ducked=f.actor.ducked,
                spectator=f.spectator, spectator_target=f.spectator_target,
                glasses=normalize_glasses(s.logic.player_runtime.glasses_style if f.entity is None else f.glasses_style),
                hp=s.health(f), alive=f.alive, frags=f.frags, deaths=f.deaths,
                carrying=f.carrying, captures=f.captures,
                respawn=f.respawn_at, protection=f.protected_until, shoot_until=f.shoot_until,
                weapons=sorted(supply.weapons) if supply else sorted(s.logic.combat_runtime.weapons),
                ammo=supply.ammo if supply else s.logic.combat_runtime.player_ammo,
                armor=supply.armor if supply else s.logic.player_runtime.player_armor,
                weapon=getattr(supply, "active", "gun1") if supply else s.logic.combat_runtime.active_weapon))
        return dict(type="snapshot", clock=s.clock, finished=s.finished, reason=s.reason,
            winner=s.result_winner, scores=s.game.scores, actors=actors,
            limit=s.limit, captures=s.game.capture_limit, paused=s.logic.session_runtime.world_paused,
            feed=[text for _, text in s.feed],
            flags=[dict(team=f.team, pos=f.pos, carrier=f.carrier, dropped=f.dropped)
                   for f in s.game.flags.values()],
            pickups=[(p.properties["id"], bool(p.properties.get("collect_collected")))
                     for p in s.logic.prop_runtime.props])

    def close(self):
        self.transport.close()


class ClientGame:
    is_client = True

    def __init__(self, session, transport, welcome):
        you, name = handshake(welcome)
        self.session, self.transport, self.you = session, transport, you
        self.pending_spectate = None
        self.elapsed, self.shot = 0, None
        self.disconnected = False
        s = session
        # These map-authored bots are represented by authoritative snapshots.
        for fighter in list(s.fighters.values()):
            if fighter.entity is not None:
                s.remove_bot(fighter.key)
        s.fighters["player"].name = name
        s.network = self
        s._hook(s.logic.prop_runtime, "_collect", lambda original, prop, **kw: False)

    def fire(self):
        actor = self.session.logic.player_runtime.player
        self.shot = dict(angle=actor.angle, pitch=actor.pitch,
                         weapon=self.session.logic.combat_runtime.active_weapon)

    def tick(self, dt):
        s = self.session
        for packet in self.transport.drain():
            if packet.get("type") == "snapshot":
                try:
                    self.apply(packet)
                except (ValueError, KeyError, TypeError, OverflowError):
                    self.disconnected = True
                    self.transport.close()
                    s.finished, s.reason, s.result_winner = True, "Invalid host snapshot", "Disconnected"
                    s.publish()
            elif packet.get("type") in ("error", "disconnected"):
                self.disconnected = True
                s.finished, s.reason, s.result_winner = True, "Host disconnected", "Disconnected"
                s.publish()
        self.elapsed += dt
        if self.elapsed >= 0.05 and not self.disconnected:
            self.elapsed = 0
            actor = s.logic.player_runtime.player
            keys = s.logic.game_state.get_keys()
            shot = self.shot or {}
            self.transport.send(dict(type="input", keys=list(keys & {0x57, 0x53, 0x41, 0x44, 0x20, 0x43, 0x01000020}),
                glasses=s.logic.player_runtime.glasses_style,
                angle=shot.get("angle", actor.angle), pitch=shot.get("pitch", actor.pitch),
                weapon=shot.get("weapon", s.logic.combat_runtime.active_weapon),
                shoot=self.shot is not None, use=0x45 in keys))
            self.shot = None

    def apply(self, packet):
        import glm
        from engine.player import Player
        from .runtime import Fighter
        s = self.session
        packet = clean_snapshot(packet)   # Raises before anything is touched.
        if packet is None:
            return
        mapping = lambda key: "player" if key == self.you else "host" if key == "player" else key
        seen, topology = {"player"}, False
        for record in packet["actors"]:
            key = mapping(record["key"])
            seen.add(key)
            if key == 'player' and self.pending_spectate is not None:
                if bool(record.get('spectator', False)) != self.pending_spectate:
                    continue
                self.pending_spectate = None
            f = s.fighters.get(key)
            if f is None:
                entity = DeathmatchBot(properties={"id": key, "name": record["name"]})
                actor = Player(0, 0)
                f = Fighter(key, record["name"], entity, actor, role=record["role"])
                s.fighters[key] = f
                s.logic.editor_state.things.append(entity)
                s._spawned.append(entity)
                topology = True
            s.game.assign(f, record["team"])
            f.glasses_style = normalize_glasses(record.get('glasses'))
            was_spectator = f.spectator
            spectator = bool(record.get('spectator', False))
            target = record.get('spectator_target')
            target = mapping(target) if target else None
            if key == 'player' and (spectator != f.spectator or target != f.spectator_target):
                s.spectators.set(f, spectator, target)
            else:
                f.spectator, f.spectator_target = spectator, target
            server_pos = glm.vec3(record["pos"])
            if key == "player":
                if not f.alive and record["alive"]:
                    f.actor.set_ducked(False)
                # Local movement prediction with bounded authoritative correction.
                error = glm.length(f.actor.pos - server_pos)
                f.actor.pos = server_pos if error > 80 or (not f.alive and not f.spectator) else f.actor.pos * 0.5 + server_pos * 0.5
                rt, combat = s.logic.player_runtime, s.logic.combat_runtime
                rt.player_health, rt.player_dead, rt.player_armor = record["hp"], not record["alive"] and not spectator, record["armor"]
                combat.weapons, combat.active_weapon, combat.player_ammo = set(record["weapons"]), record["weapon"], record["ammo"]
            else:
                f.actor.set_ducked(record.get("ducked", False))
                f.actor.pos = server_pos
                f.actor.angle, f.actor.pitch = record["angle"], record["pitch"]
                s.sync_entity_position(f)
                f.entity.properties.update(health=record["hp"], dead=not record["alive"], hidden=not record["alive"], disabled=True)
                if was_spectator != spectator:
                    from engine.change_journal import touch
                    touch(f.entity)
            f.alive, f.frags, f.deaths = record["alive"], record["frags"], record["deaths"]
            f.carrying, f.captures = record["carrying"], record["captures"]
            f.respawn_at, f.protected_until = record["respawn"], record["protection"]
            f.shoot_until = record.get("shoot_until", 0.0)
        for key, f in list(s.fighters.items()):
            if key not in seen and f.entity is not None:
                if f.entity in s.logic.editor_state.things:
                    s.logic.editor_state.things.remove(f.entity)
                if f.entity in s._spawned:
                    s._spawned.remove(f.entity)
                s.fighters.pop(key)
                topology = True
        if packet["pickups"]:
            props = {p.properties.get("id"): p for p in s.logic.prop_runtime.props}
            for identifier, collected in packet["pickups"]:
                prop = props.get(identifier)
                if prop is not None:
                    prop.properties["collect_collected"] = collected
        for info in packet["flags"]:
            flag = s.game.flags.get(info["team"])
            if flag:
                flag.pos, flag.dropped = tuple(info["pos"]), info["dropped"]
                flag.carrier = mapping(info["carrier"]) if info["carrier"] else None
                s.game.draw(flag)
        s.clock, s.finished, s.reason = packet["clock"], packet["finished"], packet["reason"]
        s.result_winner, s.game.scores = packet["winner"], dict(packet["scores"])
        s.limit, s.game.capture_limit = packet["limit"], packet["captures"]
        s.feed = [(s.clock + 5, text) for text in packet["feed"]]
        s.logic.session_runtime.set_world_paused("lan_host", packet["paused"])
        if topology:
            s.logic.world_runtime.build_entity_caches()
        s.publish()

    def close(self):
        self.session.logic.session_runtime.set_world_paused("lan_host", False)
        self.transport.close()
