# SPDX-License-Identifier: GPL-2.0-or-later
"""Host-authoritative LAN actors and snapshots, pumped on Fio's logic thread."""
import math
from engine.glasses import normalize_glasses

from .lan import PROTOCOL
from .entities import DeathmatchBot
from .supplies import BotSupplies


def finite(value, fallback=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else fallback
    except (ValueError, TypeError):
        return fallback


class HostGame:
    is_client = False

    def __init__(self, session, transport, map_data, name="Host"):
        self.session, self.transport, self.map_data = session, transport, map_data
        self.peers = {}
        self.inputs = {}
        self.elapsed = 0
        session.fighters["player"].name = name
        session.network = self

    def tick(self, dt):
        s = self.session
        for event in self.transport.drain():
            kind, key, *data = event
            if kind == "disconnected":
                fighter = self.peers.pop(key, None)
                self.inputs.pop(key, None)
                if fighter is not None:
                    s.remove_bot(fighter.key, allow_remote=True)
            elif kind == "message":
                packet = data[0]
                if packet.get("type") == "hello" and key not in self.peers:
                    if packet.get("protocol") != PROTOCOL:
                        self.transport.send(key, dict(type="error", message="Incompatible Fio arena protocol."))
                        continue
                    name = str(packet.get("name", "Player"))[:32].strip() or "Player"
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
        self.session, self.transport, self.you = session, transport, welcome["you"]
        self.pending_spectate = None
        self.elapsed, self.shot = 0, None
        self.disconnected = False
        s = session
        # These map-authored bots are represented by authoritative snapshots.
        for fighter in list(s.fighters.values()):
            if fighter.entity is not None:
                s.remove_bot(fighter.key)
        s.fighters["player"].name = welcome.get("name", "You")
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
        mapping = lambda key: "player" if key == self.you else "host" if key == "player" else key
        seen, topology = {"player"}, False
        records = packet.get("actors", [])
        if not isinstance(records, list) or len(records) > 128:
            return
        for record in records:
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
                s.fighters.pop(key)
                topology = True
        for identifier, collected in packet.get("pickups", []):
            prop = next((p for p in s.logic.prop_runtime.props if p.properties["id"] == identifier), None)
            if prop is not None:
                prop.properties["collect_collected"] = collected
        for info in packet.get("flags", []):
            flag = s.game.flags.get(info["team"])
            if flag:
                flag.pos, flag.dropped = tuple(info["pos"]), info["dropped"]
                flag.carrier = mapping(info["carrier"]) if info["carrier"] else None
                s.game.draw(flag)
        s.clock, s.finished, s.reason = packet["clock"], packet["finished"], packet["reason"]
        s.result_winner, s.game.scores = packet["winner"], dict(packet["scores"])
        s.limit, s.game.capture_limit = packet["limit"], packet["captures"]
        s.feed = [(s.clock + 5, text) for text in packet.get("feed", [])]
        s.logic.session_runtime.set_world_paused("lan_host", packet.get("paused", False))
        if topology:
            s.logic.world_runtime.build_entity_caches()
        s.publish()

    def close(self):
        self.session.logic.session_runtime.set_world_paused("lan_host", False)
        self.transport.close()
