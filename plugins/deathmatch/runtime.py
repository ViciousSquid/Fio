"""Desktop-engine Deathmatch session. All simulation stays on the logic thread.

Bots reuse Player's hull, gravity, steps and water movement. Native Monster
identity keeps hitscan, melee, projectiles and billboard rendering working.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import math
import random

from .entities import DeathmatchBot, DeathmatchNode, DeathmatchSettings, DeathmatchSpawn
from .navigation import Navigation, MoverTraversal, distance
from .moving_navigation import NavigationWorld, live_centre
from .supplies import BotSupplies
from .spawning import choose_spawn
from .teams import TeamGame


SKILLS = {
    # reaction seconds, aim error degrees, minimum shot interval
    "easy": (0.65, 8.0, 0.85),
    "normal": (0.35, 4.0, 0.60),
    "hard": (0.18, 1.5, 0.42),
}


def number(props, key, default, low=0.0, high=100000.0):
    try:
        value = float(props.get(key, default))
        return max(low, min(high, value)) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


@dataclass
class Fighter:
    key: str
    name: str
    entity: object = None
    actor: object = None
    frags: int = 0
    deaths: int = 0
    alive: bool = True
    respawn_at: float = 0.0
    protected_until: float = 0.0
    target: str | None = None
    last_seen: tuple | None = None
    memory_until: float = 0.0
    visible: bool = False
    think_at: float = 0.0
    fire_at: float = 0.0
    shoot_until: float = 0.0
    route_at: float = 0.0
    route: list = field(default_factory=list)
    strafe: float = 1.0
    stuck: float = 0.0
    jump_held: bool = False
    escape_until: float = 0.0
    escape_angle: float = 0.0
    aim_error: tuple = (0.0, 0.0)
    mode: str = "roam"
    supplies: object = None
    team: str = ""
    carrying: str | None = None
    captures: int = 0
    objective_score: int = 0
    role: str = "bot"
    peer: str | None = None
    traversal: object = None
    navigation_reason: str = ""
    spectator: bool = False
    spectator_target: str | None = None
    glasses_style: str = 'classic'


class DeathmatchSession:
    def __init__(self, logic, host=None):
        self.logic, self.host = logic, host
        self.clock = 0.0
        self.finished = False
        self.result_winner = None
        self.reason = ""
        self.restart_requested = False
        self._restart_key = False
        self._hooks = []
        self._authored = []
        self._spawned = []
        self._removed = []
        self._human_attacker = None
        self._started = False
        self.snapshot = {}
        self.feed = []
        self.noises = []
        self.fighters = {}
        self.network = None

    @staticmethod
    def supported(logic):
        return (callable(getattr(logic, "_tick_play_mode", None))
                and getattr(logic, "trigger_runtime", None) is not None
                and getattr(logic, "collision_runtime", None) is not None
                and getattr(logic.player_runtime, "player", None) is not None)

    def start(self):
        import glm
        from engine.player import Player

        things = self.logic.editor_state.things
        self.settings = next((t for t in things if isinstance(t, DeathmatchSettings)), None)
        if self.settings is None:
            return False  # Enabling the plugin alone never changes ordinary maps.
        self.map_data = self.logic.editor_state.get_level_data()
        props = self.settings.properties
        self.game = TeamGame(self)
        self.limit = int(number(props, "frag_limit", 50, 0, 10000))
        self.duration = number(props, "time_limit", 1800, 0, 86400)
        self.delay = number(props, "respawn_delay", 3, 0.1, 30)
        self.protection = number(props, "spawn_protection", 1.5, 0, 10)
        self.rng = random.Random(int(number(props, "seed", 42, -2147483648, 2147483647)))
        self.spawns = [(tuple(t.pos), math.radians(number(t.properties, "angle", 0, -360, 360)))
                       for t in things if isinstance(t, DeathmatchSpawn)]
        self.spawn_teams = {tuple(t.pos): t.properties.get("team", "any")
                            for t in things if isinstance(t, DeathmatchSpawn)}
        rt = self.logic.player_runtime
        self.fighters["player"] = Fighter("player", "You", actor=rt.player, role="player")
        self.game.assign(self.fighters["player"], props.get("player_team", "red"))
        self._player_state = {key: copy.deepcopy(getattr(rt, key)) for key in (
            "player_health", "player_max_health", "player_armor", "player_dead")}
        combat = self.logic.combat_runtime
        self._loadout = (set(combat.weapons), combat.active_weapon, combat.player_ammo)
        self._player_pose = (tuple(rt.player.pos), rt.player.angle, rt.player.pitch)
        self.hull = (rt.player.width / 2, rt.player.height / 2, rt.player.depth / 2)
        self.nav_world = NavigationWorld(self.logic, self.hull, rt.player.step_height)
        self.nav = Navigation([t for t in things if isinstance(t, DeathmatchNode)],
                              project=self.navigation_point, surfaces=self.nav_world.surfaces,
                              transition=self.nav_world.transfer)
        self._started = True
        for entity in things:
            if not isinstance(entity, DeathmatchBot):
                continue
            self._authored.append((entity, list(entity.pos), copy.deepcopy(entity.properties)))
            actor = Player(entity.pos[0], entity.pos[2])
            actor.pos = glm.vec3(entity.pos) + glm.vec3(0, actor.height / 2, 0)
            key = str(entity.properties["id"])
            self.fighters[key] = Fighter(key, entity.name, entity, actor,
                                         strafe=self.rng.choice((-1.0, 1.0)))
            self.game.assign(self.fighters[key], entity.properties.get("team", "auto"))
            # Reserved while the mode runs: stock AI must not drive these bots.
            entity.properties["disabled"] = True
        if not self.spawns:
            self.spawns = [(self.feet(f), f.actor.angle) for f in self.fighters.values()]
        self._install_hooks()
        self._pickup_initial = {id(p): p.properties.get("collect_collected", False)
                                for p in self.logic.prop_runtime.props}
        self.restart()
        return True

    def _hook(self, obj, name, callback):
        original = getattr(obj, name)

        def wrapped(*args, **kwargs):
            return callback(original, *args, **kwargs)

        self._hooks.append((obj, name, original, wrapped))
        setattr(obj, name, wrapped)

    def _install_hooks(self):
        from .spectator import Spectators
        self.spectators = Spectators(self)
        self._hook(self.logic, "_tick_play_mode", self._engine_tick)
        self._hook(self.logic.combat_runtime, "_damage_monster", self._player_hit)
        self._hook(self.logic.monster_ai, "_apply_monster_damage", self._monster_hit)
        self._hook(self.logic.trigger_runtime, "_apply_player_damage", self._human_hit)
        self._hook(self.logic.combat_runtime, "_handle_shooting", self._player_fire)
        self.spectators.install()

    def _engine_tick(self, original, delta):
        was_dead = self.logic.player_runtime.player_dead
        riders = [(f, f.actor.ground_object, tuple(live_centre(f.actor.ground_object)))
                  for f in self.fighters.values() if f.entity is not None and f.alive
                  and f.actor.on_ground and isinstance(f.actor.ground_object, dict)
                  and (f.actor.ground_object.get('is_mover') or f.actor.ground_object.get('is_door'))]
        original(delta)
        if not self._started:
            return
        # Native movers carry the local player. Carry the other Player
        # controllers by the same actual translation, once, before snapshots.
        import glm
        live = {id(b) for b in self.logic.editor_state.brushes}
        for fighter, support, before in riders:
            if (fighter.alive and id(support) in live and fighter.actor.on_ground
                    and fighter.actor.ground_object is support):
                fighter.actor.pos += glm.vec3(live_centre(support)) - glm.vec3(before)
                self.sync_entity_position(fighter)
        if self.network is not None:
            with self.logic.session_runtime.monster_lock:
                self.network.tick(max(0, min(float(delta), 0.1)))
            if self.network.is_client:
                if self.fighters['player'].spectator:
                    self.spectators.follow(self.fighters['player'])
                return
        # Unlike Fio's regular plugin tick, this also runs during the native
        # death screen, so the respawn clock and surviving bots keep moving.
        if (self.logic.session_runtime.world_paused or self.logic.cutscene_runtime.state
                or self.logic.interaction_runtime.level_complete_ui):
            return
        with self.logic.session_runtime.monster_lock:
            if was_dead:
                self.logic.prop_runtime._update_respawns(delta)
                self.logic.prop_runtime._update_respawn_fades(delta)
            self.step(delta)

    def _player_fire(self, original):
        if self.fighters['player'].spectator:
            return
        if self.network is not None and self.network.is_client:
            import time
            combat = self.logic.combat_runtime
            if combat.shot_ready(time.perf_counter()) and not self.finished:
                self.network.fire()
                combat._last_player_shot_time = time.perf_counter()
            return
        fighter = self.fighters["player"]
        if self.finished or not fighter.alive:
            return
        import time
        if self.logic.combat_runtime.shot_ready(time.perf_counter()):
            fighter.protected_until = 0
            self.noises.append((self.clock, self.feet(fighter), fighter.key))
        return original()

    def _fighter_for(self, entity):
        if entity is None:
            return None
        return self.fighters.get(str(entity.properties.get("id", "")))

    def _can_damage(self, fighter):
        return (not self.finished and fighter.alive
                and self.clock >= fighter.protected_until)

    def _player_hit(self, original, weapon, entity):
        if self.network is not None and self.network.is_client:
            return False
        victim = self._fighter_for(entity)
        if victim is None:
            return original(weapon, entity)
        if not self._can_damage(victim) or not self.game.can_damage(self.fighters["player"], victim):
            return False
        if victim.supplies is not None:
            from dataclasses import replace
            weapon = replace(weapon, damage=self.absorb(victim, weapon.damage))
        result = original(weapon, entity)
        if entity.properties.get("dead"):
            self.kill(victim, self.fighters["player"])
        return result

    def _monster_hit(self, original, entity, damage, attacker=None):
        victim = self._fighter_for(entity)
        if victim is None:
            return original(entity, damage, attacker=attacker)
        killer = self._fighter_for(attacker) or self.projectile_attacker()
        if not self._can_damage(victim) or not self.game.can_damage(killer, victim):
            return
        result = original(entity, self.absorb(victim, damage), attacker=attacker)
        if entity.properties.get("dead"):
            self.kill(victim, killer)
        return result

    def absorb(self, fighter, damage):
        if fighter.supplies is None:
            return damage
        absorbed = min(fighter.supplies.armor, max(0, damage))
        fighter.supplies.armor -= absorbed
        return damage - absorbed

    def projectile_attacker(self):
        owner = self.logic.combat_runtime.projectile_damage_owner
        if owner == 0:
            return self.fighters["player"]
        return next((f for f in self.fighters.values() if f.entity is not None and id(f.entity) == owner), None)

    def _human_hit(self, original, damage):
        if self.network is not None and self.network.is_client:
            return
        victim = self.fighters["player"]
        if not self._can_damage(victim) or not self.game.can_damage(self._human_attacker or self.projectile_attacker(), victim):
            return
        result = original(damage)
        if self.logic.player_runtime.player_health <= 0:
            self.logic.player_runtime.player_dead = True
            self.kill(victim, self._human_attacker or self.projectile_attacker())
        return result

    def fire_output(self, entity, name, value=None):
        if self.logic.io_manager is not None:
            self.logic.io_manager.fire_output(entity, name, value)

    def emit(self, name, **data):
        if self.host is not None:
            self.host.emit(name, logic=self.logic, **data)

    def kill(self, victim, killer=None):
        if self.finished or not victim.alive:
            return  # Multi-pellet weapons and repeated damage count once.
        victim.alive = False
        victim.deaths += 1
        victim.respawn_at = self.clock + self.delay
        victim.target, victim.route = None, []
        victim.traversal = None
        was_carrier = victim.carrying is not None
        self.game.drop(victim)
        if killer is not None and killer is not victim:
            friendly = self.game.allies(killer, victim)
            killer.frags += -1 if friendly else 1
            if self.game.mode == "teamdeathmatch":
                self.game.scores[killer.team] += -1 if friendly else 1
            if was_carrier and not friendly:
                killer.objective_score += 2
        else:
            victim.frags -= 1
            if self.game.mode == "teamdeathmatch":
                self.game.scores[victim.team] -= 1
        if victim.entity is not None:
            victim.entity.properties.update(dead=True, hidden=True, is_shooting=False)
        text = f"{killer.name if killer else 'World'} > {victim.name}"
        self.feed.append((self.clock + 5, text))
        self.fire_output(self.settings, "OnFrag", text)
        self.emit("deathmatch.frag", victim=victim.key,
                  killer=killer.key if killer else None)
        self.check_frag_limit()

    def check_frag_limit(self):
        if not self.limit or self.game.ctf:
            return
        values = self.game.scores.values() if self.game.enabled else (f.frags for f in self.fighters.values() if not f.spectator)
        if any(v >= self.limit for v in values):
            self.finish("Frag limit")

    def finish(self, reason):
        if self.finished:
            return
        self.finished, self.reason = True, reason
        for fighter in self.fighters.values():
            if fighter.entity is not None:
                fighter.entity.properties["is_shooting"] = False
        leaders = self.leaders()
        winner = self.game.winner() or ("Draw" if len(leaders) != 1 else leaders[0].name)
        self.result_winner = winner
        self.fire_output(self.settings, "OnMatchEnd", winner)
        self.emit("deathmatch.end", winner=winner, reason=reason)
        self.publish()

    def leaders(self):
        participants = [f for f in self.fighters.values() if not f.spectator]
        best = max((f.frags for f in participants), default=0)
        return [f for f in participants if f.frags == best]

    def restart(self):
        self.clock, self.finished, self.reason = 0.0, False, ""
        self.result_winner = None
        self.feed.clear()
        self.noises.clear()
        self.restart_requested = False
        self.game.reset()
        self.logic.combat_runtime._monster_projectiles.clear()
        for prop in self.logic.prop_runtime.props:
            if prop.properties.get("collect_collected") and not self._pickup_initial.get(id(prop), False):
                self.logic.prop_runtime.respawn_prop(prop)
        # Mark everyone out before selecting spawns, then reserve each spawn
        # with the fighters already placed there.
        for fighter in self.fighters.values():
            fighter.alive = False
            fighter.frags = fighter.deaths = 0
        for fighter in self.fighters.values():
            self.respawn(fighter)
        self.fire_output(self.settings, "OnMatchStart")
        self.emit("deathmatch.start")
        self.publish()

    def feet(self, fighter):
        p = fighter.actor.pos
        return (float(p[0]), float(p[1]) - fighter.actor.height / 2, float(p[2]))

    def sync_entity_position(self, fighter):
        """Centre the billboard above the controller's feet, using its own size."""
        from engine.entity_table import sprite_size
        x, y, z = self.feet(fighter)
        fighter.entity.pos = [x, y + sprite_size(fighter.entity)[1] / 2, z]

    def eye(self, fighter):
        p = fighter.actor.pos
        return (float(p[0]), float(p[1]) + fighter.actor.camera_height, float(p[2]))

    def spawn_choice(self, fighter):
        return choose_spawn(self, fighter)

    def respawn(self, fighter):
        if fighter.spectator:
            return
        import glm
        pos, angle = self.spawn_choice(fighter)
        fighter.actor.set_ducked(False)
        fighter.actor.pos = glm.vec3(pos) + glm.vec3(0, fighter.actor.height / 2, 0)
        fighter.actor.velocity = glm.vec3(0)
        fighter.actor.angle, fighter.actor.pitch = angle, 0.0
        fighter.actor.on_ground = False
        fighter.alive = True
        fighter.protected_until = self.clock + self.protection
        fighter.target, fighter.last_seen, fighter.route = None, None, []
        fighter.think_at = self.clock + self.rng.uniform(0, 0.15)
        fighter.fire_at = self.clock + 0.5
        fighter.shoot_until = 0.0
        fighter.memory_until = fighter.route_at = fighter.stuck = 0.0
        fighter.escape_until = 0.0
        fighter.traversal, fighter.navigation_reason = None, ""
        fighter.jump_held = False
        fighter.mode, fighter.visible = "roam", False
        if fighter.entity is None:
            rt = self.logic.player_runtime
            rt.player_dead, rt.player_health, rt.player_armor = False, rt.player_max_health, 0
            fighter.actor.set_ducked(False)
            self.give_loadout()
        else:
            fighter.supplies = BotSupplies(self, fighter)
            if fighter.role == "remote":
                fighter.supplies.weapons = {"gun1", "gun2"}
                fighter.supplies.ammo = 24
                fighter.fire_at = self.clock
            self.sync_entity_position(fighter)
            fighter.entity.properties.update(
                health=int(number(fighter.entity.properties, "max_health", 100, 1, 10000)),
                dead=False, hidden=False, disabled=True, is_shooting=False)
        self.emit("deathmatch.respawn", fighter=fighter.key)

    def give_loadout(self):
        combat = self.logic.combat_runtime
        for item_id in ("gun2", "gun1"):
            item = combat.items.resolve(item_id)
            if item is not None and item.weapon is not None:
                combat.give_weapon(item)
        combat.player_ammo = max(combat.player_ammo, 24)

    def add_bot(self, name=None, skill="normal", team="auto"):
        """Called with the tick and monster locks held by the console handler."""
        from engine.player import Player
        existing = {f.name.casefold() for f in self.fighters.values()}
        if name is None:
            index = 1
            while f"Bot {index}".casefold() in existing:
                index += 1
            name = f"Bot {index}"
        if not name.strip() or len(name) > 48:
            return "Bot names must contain 1–48 characters."
        if name.casefold() in existing:
            return f"A participant named {name} already exists."
        entity = DeathmatchBot(properties={"name": name, "skill": skill})
        actor = Player(0, 0)
        key = entity.properties["id"]
        fighter = Fighter(key, name, entity, actor, strafe=self.rng.choice((-1.0, 1.0)))
        self.fighters[key] = fighter
        self.game.assign(fighter, team)
        self._spawned.append(entity)
        self.logic.editor_state.things.append(entity)
        self.respawn(fighter)
        self.logic.world_runtime.build_entity_caches()
        self.emit("entity_spawned", entity=entity, by="deathmatch")
        self.publish()
        return f"Added {name} ({skill})."

    def remove_bot(self, name=None, *, allow_remote=False):
        bots = [f for f in self.fighters.values() if f.entity is not None and (allow_remote or f.role == "bot")]
        if not bots:
            return "No bots to remove."
        if name is None:
            fighter = bots[-1]
        else:
            matches = [f for f in bots if f.key == name or f.name.casefold() == name.casefold()]
            if not matches:
                return f"No bot named {name}."
            if len(matches) != 1:
                return "Several bots have that name; use the bot's entity ID."
            fighter = matches[0]
        entity = fighter.entity
        self.game.drop(fighter)
        scene = self.logic.editor_state.things
        if entity in scene:
            if entity not in self._spawned:
                # Restore authored membership and order when Play ends.
                self._removed.append((entity, scene.index(entity)))
            scene.remove(entity)
        self.fighters.pop(fighter.key)
        self.noises = [n for n in self.noises if n[2] != fighter.key]
        for other in self.fighters.values():
            if other.target == fighter.key:
                other.target, other.last_seen, other.route = None, None, []
                other.visible = False
        self.logic.world_runtime.build_entity_caches()
        self.publish()
        return f"Removed {fighter.name}."

    def set_frag_limit(self, value):
        self.limit = value
        if not self.finished:
            self.check_frag_limit()
        self.publish()
        return "Frag limit: unlimited." if value == 0 else f"Frag limit: {value}."

    def visible_points(self, start, end):
        import glm
        offset = glm.vec3(end) - glm.vec3(start)
        reach = glm.length(offset)
        if reach < 0.01:
            return True
        combat = self.logic.combat_runtime
        dist, _ = combat._trace_brushes(self.logic.collision_runtime.collision_rows(),
                                       glm.vec3(start), offset / reach, reach)
        return dist >= reach - 0.1

    def clear(self, start, end):
        """Conservative full-hull corridor, including current movers/doors.

        This is a flat waypoint planner. Physics still handles slopes/steps;
        Marker heights are projected separately; walking segments must have
        supporting floors and must never be authored across drops.
        """
        return self.nav_world.clear(start, end)

    def walkable(self, start, end):
        return self.nav_world.walkable(start, end)

    def navigation_point(self, marker, reference):
        return self.nav_world.project(marker, reference)

    def plan_route(self, fighter, goal):
        route = self.nav.route(self.feet(fighter), goal, self.walkable)
        fighter.navigation_reason = '' if route else self.nav_world.blocker(self.feet(fighter), goal)
        return route

    def follow_route(self, fighter, route, radius=36):
        self.nav.advance(route, self.feet(fighter), self.walkable, default_radius=radius)
        if not route:
            if fighter.navigation_reason:
                fighter.mode = 'waiting for route'
            return None
        point = route[0]
        if isinstance(point, MoverTraversal):
            fighter.traversal = point
            point.route = route
            return self.traversal_target(fighter)
        if self.walkable(self.feet(fighter), point):
            fighter.navigation_reason = ""
            return point
        fighter.navigation_reason = self.nav_world.blocker(self.feet(fighter), point)
        fighter.mode = 'waiting for route'
        route.clear()
        fighter.route_at = self.clock + 0.25
        return None

    def traversal_target(self, fighter):
        action = fighter.traversal
        mover, pos = action.mover, self.feet(fighter)
        name = mover.get('name') or 'platform'
        if (not any(b is mover for b in self.logic.editor_state.brushes)
                or mover.get('hidden') or mover.get('disabled')):
            fighter.traversal = None
            action.route.clear()
            fighter.route_at = self.clock
            fighter.navigation_reason = 'Mover no longer available; replanning'
            return None
        aboard = fighter.actor.on_ground and fighter.actor.ground_object is mover
        if aboard and action.stage not in ('disembark',):
            action.stage = 'ride'
        if action.stage == 'approach':
            if math.hypot(pos[0]-action.landing[0], pos[2]-action.landing[2]) > 12:
                fighter.mode = 'approaching mover'
                return action.landing if self.walkable(pos, action.landing) else None
            action.stage = 'wait'
        current = tuple(live_centre(mover))
        standing = (current[0]+action.offset[0], current[1]+mover['size'][1]/2,
                    current[2]+action.offset[2])
        if action.stage in ('wait', 'board'):
            near_stop = (abs(current[1]-action.board[1]) <= fighter.actor.step_height+1
                         and math.hypot(current[0]-action.board[0], current[2]-action.board[2]) <= 32)
            if near_stop and self.walkable(pos, standing):
                action.stage = 'board'
                fighter.mode = 'boarding mover'
                fighter.navigation_reason = f'Boarding {name}'
                return standing
            fighter.mode = 'waiting for mover'
            fighter.navigation_reason = f'Waiting for {name} at the boarding stop'
            return None
        if action.stage == 'ride':
            if not aboard and fighter.actor.on_ground:
                fighter.traversal = None
                action.route.clear()
                fighter.route_at = self.clock
                fighter.navigation_reason = 'Left mover before destination; replanning'
                return None
            if self.walkable(pos, action.target):
                action.stage = 'disembark'
            else:
                fighter.mode = 'riding mover'
                fighter.navigation_reason = f'Riding {name}; waiting for a supported exit'
                return standing if math.hypot(pos[0]-standing[0], pos[2]-standing[2]) > 6 else None
        if action.stage == 'disembark':
            if not aboard and abs(pos[1]-action.target[1]) <= 2:
                fighter.traversal = None
                if action.route and action.route[0] is action:
                    action.route.pop(0)
                fighter.navigation_reason = f'Disembarked from {name}'
                return None
            if self.walkable(pos, action.target):
                fighter.mode = 'leaving mover'
                return action.target
            if aboard:
                action.stage = 'ride'
                fighter.navigation_reason = self.nav_world.blocker(pos, action.target)
            return None

    def health(self, fighter):
        return (self.logic.player_runtime.player_health if fighter.entity is None
                else fighter.entity.properties["health"])

    def perceive(self, fighter):
        props = fighter.entity.properties
        vision = number(props, "vision_range", 1800, 64, 10000)
        half_fov = math.radians(number(props, "field_of_view", 160, 30, 360)) / 2
        seen = []
        for other in self.fighters.values():
            if other is fighter or not other.alive or self.game.allies(fighter, other):
                continue
            dist = distance(self.eye(fighter), self.eye(other))
            if dist > vision:
                continue
            delta = (other.actor.pos[0] - fighter.actor.pos[0],
                     other.actor.pos[2] - fighter.actor.pos[2])
            heading = math.atan2(delta[0], delta[1])
            angle = abs((heading - fighter.actor.angle + math.pi) % (2 * math.pi) - math.pi)
            if angle > half_fov and dist > 180:
                continue
            if self.visible_points(self.eye(fighter), self.eye(other)):
                # Keep a visible opponent unless another is substantially nearer.
                seen.append((dist * (0.70 if other.key == fighter.target else 1), other.key))
        fighter.visible = bool(seen)
        if seen:
            _, key = min(seen)
            target = self.fighters[key]
            if key != fighter.target:
                fighter.fire_at = max(fighter.fire_at, self.clock + self.skill(fighter)[0])
            fighter.target, fighter.last_seen = key, self.feet(target)
            fighter.memory_until = self.clock + 4.0
            error = self.skill(fighter)[1]
            fighter.aim_error = (math.radians(self.rng.uniform(-error, error)),
                                 math.radians(self.rng.uniform(-error, error)))
        elif self.clock >= fighter.memory_until:
            fighter.target = None
            fighter.last_seen = None
            hearing = number(props, "hearing_range", 1000, 0, 10000)
            for at, pos, source in reversed(self.noises):
                if source != fighter.key and self.clock - at < 1.5 and distance(pos, self.feet(fighter)) < hearing:
                    fighter.last_seen, fighter.memory_until = pos, self.clock + 3
                    break

    def skill(self, fighter):
        return SKILLS.get(str(fighter.entity.properties.get("skill", "normal")).lower(), SKILLS["normal"])

    def update_bot(self, fighter, dt):
        import glm
        if self.clock >= fighter.think_at:
            self.perceive(fighter)
            fighter.think_at = self.clock + 0.15
            if self.rng.random() < 0.12:
                fighter.strafe *= -1
        target = self.fighters.get(fighter.target)
        visible = (fighter.visible and target is not None and target.alive
                   and self.visible_points(self.eye(fighter), self.eye(target)))
        pos = self.feet(fighter)
        supply_goal = fighter.supplies.goal(visible)
        objective = self.game.objective(fighter)
        retreat = self.health(fighter) < number(fighter.entity.properties, "max_health", 100, 1) * 0.3
        fighter.mode = "retreat" if retreat and visible else "fight" if visible else "search" if fighter.last_seen else "roam"
        goal = fighter.last_seen
        if fighter.traversal is not None:
            waypoint = self.traversal_target(fighter)
            move = glm.vec3(0)
            if waypoint is not None:
                fighter.actor.angle = math.atan2(waypoint[0]-pos[0], waypoint[2]-pos[2])
                fighter.actor.pitch = 0
                move.z = 1 if math.hypot(waypoint[0]-pos[0], waypoint[2]-pos[2]) > 4 else 0
        elif supply_goal is not None:
            fighter.mode = "resupply"
            supply_goal = self.follow_route(fighter, fighter.supplies.route, 30)
            fighter.actor.angle = math.atan2(supply_goal[0] - pos[0], supply_goal[2] - pos[2]) if supply_goal else fighter.actor.angle
            fighter.actor.pitch = 0
            prop = fighter.supplies.target
            if prop is not None and distance(self.eye(fighter), prop.pos) < 80:
                delta = glm.vec3(prop.pos) - glm.vec3(self.eye(fighter))
                fighter.actor.pitch = math.atan2(delta.y, math.hypot(delta.x, delta.z))
            move = glm.vec3(0, 0, 1) if supply_goal is not None and distance(pos, supply_goal) > 8 else glm.vec3(0)
        elif objective is not None:
            fighter.mode = "flag carrier" if fighter.carrying else "objective"
            if self.clock >= fighter.route_at:
                fighter.route = self.plan_route(fighter, objective)
                fighter.route_at = self.clock + 0.5
            waypoint = self.follow_route(fighter, fighter.route, 25)
            move = glm.vec3(0)
            if waypoint is not None:
                if visible:
                    offset = glm.vec3(self.eye(target)) - glm.vec3(self.eye(fighter))
                    fighter.actor.angle = math.atan2(offset.x, offset.z) + fighter.aim_error[0]
                    fighter.actor.pitch = math.atan2(offset.y, math.hypot(offset.x, offset.z)) + fighter.aim_error[1]
                    if self.clock >= fighter.fire_at:
                        self.fire_bot(fighter)
                else:
                    fighter.actor.angle = math.atan2(waypoint[0] - pos[0], waypoint[2] - pos[2])
                    fighter.actor.pitch = 0
                heading = math.atan2(waypoint[0] - pos[0], waypoint[2] - pos[2]) - fighter.actor.angle
                move = glm.vec3(math.sin(heading), 0, math.cos(heading))
        elif visible:
            goal = self.feet(target)
            # Face the opponent while strafing/backpedalling.
            offset = glm.vec3(self.eye(target)) - glm.vec3(self.eye(fighter))
            yaw = math.atan2(offset.x, offset.z) + fighter.aim_error[0]
            turn = (yaw - fighter.actor.angle + math.pi) % (2 * math.pi) - math.pi
            fighter.actor.angle += max(-dt * 6, min(dt * 6, turn))
            fighter.actor.pitch = math.atan2(offset.y, math.hypot(offset.x, offset.z)) + fighter.aim_error[1]
            preferred = number(fighter.entity.properties, "preferred_range", 480, 100, 2000)
            forward = -0.8 if retreat or distance(pos, goal) < preferred * 0.6 else 1.0 if distance(pos, goal) > preferred else 0.0
            move = glm.vec3(0.8 * fighter.strafe, 0, forward)
            if self.clock >= fighter.fire_at and abs(turn) < 0.15:
                self.fire_bot(fighter)
        else:
            fighter.actor.pitch = 0
            if goal is None and self.nav.points:
                if not fighter.route or self.clock >= fighter.route_at:
                    name = self.rng.choice(list(self.nav.points))
                    options = self.nav.options(name, pos)
                    goal = self.rng.choice(options) if options else None
                else:
                    goal = fighter.route[-1]
            if goal is not None and (self.clock >= fighter.route_at or not fighter.route):
                fighter.route = self.plan_route(fighter, goal)
                fighter.route_at = self.clock + 1.0
            waypoint = self.follow_route(fighter, fighter.route)
            move = glm.vec3(0)
            if waypoint is not None:
                nxt = waypoint
                fighter.actor.angle = math.atan2(nxt[0] - pos[0], nxt[2] - pos[2])
                move.z = 1.0
            elif goal is None:
                # Maps without authored waypoints still get local exploration.
                fighter.actor.angle += dt * 0.35 * fighter.strafe
                move.z = 1.0
        old = glm.vec3(fighter.actor.pos)
        if self.clock < fighter.escape_until and fighter.traversal is None:
            fighter.actor.angle = fighter.escape_angle
            move = glm.vec3(0, 0, 1)
        if fighter.actor.on_ground and glm.length(move) > 0:
            forward = glm.vec3(math.sin(fighter.actor.angle), 0, math.cos(fighter.actor.angle))
            side = glm.vec3(math.cos(fighter.actor.angle), 0, -math.sin(fighter.actor.angle))
            predicted = glm.vec3(pos) + (forward*move.z+side*move.x)*max(8, dt*fighter.actor.speed)
            if not self.walkable(pos, predicted):
                move = glm.vec3(0)
        jump = fighter.stuck > 0.6 and not fighter.jump_held and fighter.traversal is None
        fighter.actor.update(
            dt, move, jump, False, self.logic.collision_runtime._collision_brushes_cache,
            self.logic.mover_runtime._mover_brush_list, self.logic.mover_runtime._door_brush_list,
            self.logic.world_runtime.terrain,
            spatial_grid=self.logic.session_runtime.spatial_grid,
        )
        fighter.jump_held = jump
        travelled = math.hypot(fighter.actor.pos.x - old.x, fighter.actor.pos.z - old.z)
        fighter.stuck = fighter.stuck + dt if glm.length(move) > 0.1 and travelled < dt * 15 else 0
        if fighter.stuck > 1.2:
            if fighter.supplies.target is not None:
                fighter.supplies.avoid_until[id(fighter.supplies.target)] = self.clock + 3
                fighter.supplies.target, fighter.supplies.route = None, []
            fighter.escape_angle = fighter.actor.angle + math.pi * 0.6 * fighter.strafe
            fighter.escape_until = self.clock + 0.7
            fighter.route.clear()
            fighter.route_at = self.clock + 0.5
            fighter.stuck = 0
        self.sync_entity_position(fighter)
        fighter.supplies.collect_nearby()
        if fighter.actor.pos.y < -4096:
            self.kill(fighter)

    def fire_bot(self, shooter, selected=None, cadence=None):
        import glm
        if not shooter.alive or self.finished:
            return
        target = self.fighters.get(shooter.target)
        target_distance = distance(self.feet(shooter), self.feet(target)) if target else 400
        if selected is not None and selected in shooter.supplies.weapons:
            item = self.logic.combat_runtime.items.resolve(selected)
            weapon = item.weapon if item is not None else None
            if weapon is not None and shooter.supplies.ammo < weapon.ammo_per_shot:
                return
        else:
            weapon = shooter.supplies.weapon(target_distance) if shooter.supplies is not None else None
        if weapon is None:
            return
        shooter.supplies.ammo -= weapon.ammo_per_shot
        shooter.fire_at = self.clock + max(weapon.cooldown, self.skill(shooter)[2] if cadence is None else cadence)
        shooter.shoot_until = self.clock + 0.12
        shooter.protected_until = 0
        self.noises.append((self.clock, self.feet(shooter), shooter.key))
        origin = glm.vec3(self.eye(shooter))
        yaw, pitch = shooter.actor.angle, shooter.actor.pitch
        direction = glm.vec3(math.sin(yaw) * math.cos(pitch), math.sin(pitch),
                             math.cos(yaw) * math.cos(pitch))
        combat = self.logic.combat_runtime
        self.logic.game_state.queue_sound({"file": weapon.sound, "volume": 0.6,
                                            "position": tuple(origin), "radius": 1200})
        if weapon.mode == "projectile":
            speed = weapon.projectile_speed
            combat._add_monster_projectile(tuple(origin), tuple(direction * speed), id(shooter.entity),
                                           weapon.damage, min(weapon.range, 2048) / speed)
            return
        for pellet in range(weapon.pellets):
            ray = combat._scatter(direction, weapon.spread, pellet)
            self.fire_ray(shooter, weapon, origin, ray)

    def fire_ray(self, shooter, weapon, origin, direction):
        import glm
        combat = self.logic.combat_runtime
        reach = weapon.range
        wall, _ = combat._trace_brushes(self.logic.collision_runtime.collision_rows(), origin, direction, reach)
        victim, nearest = None, min(wall, reach)
        for other in self.fighters.values():
            if other is shooter or not other.alive:
                continue
            center = glm.vec3(other.actor.pos)
            half = glm.vec3(other.actor.width / 2, other.actor.height / 2, other.actor.depth / 2)
            hit, dist = combat.intersect_ray_aabb(origin, direction, center - half, center + half, reach)
            if hit and dist < nearest:
                victim, nearest = other, dist
        if victim is None:
            return
        damage = weapon.damage
        if victim.entity is not None:
            self.logic.monster_ai._apply_monster_damage(victim.entity, damage, attacker=shooter.entity)
        else:
            self._human_attacker = shooter
            self.logic.player_runtime.damage_source = tuple(shooter.actor.pos)
            try:
                self.logic.trigger_runtime._apply_player_damage(damage)
            finally:
                self._human_attacker = None

    def step(self, delta):
        dt = max(0.0, min(float(delta), 0.1))
        keys = self.logic.game_state.get_keys()
        restart_key = 0x52 in keys  # R, only after the match ends.
        if self.restart_requested or (self.finished and restart_key and not self._restart_key):
            self.restart()
        self._restart_key = restart_key
        if not self.finished:
            self.clock += dt
            if self.duration and self.clock >= self.duration:
                self.finish("Time limit")
        if not self.finished:
            for fighter in self.fighters.values():
                if fighter.spectator:
                    if fighter.role == 'remote' and self.network is not None:
                        self.network.update_remote(fighter, dt)
                    continue
                if not fighter.alive:
                    if self.clock >= fighter.respawn_at:
                        self.respawn(fighter)
                elif fighter.entity is not None:
                    if fighter.entity.properties.get("dead") or fighter.entity.properties.get("health", 1) <= 0:
                        self.kill(fighter)
                    else:
                        if fighter.role == "remote" and self.network is not None:
                            self.network.update_remote(fighter, dt)
                        else:
                            self.update_bot(fighter, dt)
                elif self.logic.player_runtime.player_health <= 0 or self.logic.player_runtime.player_dead:
                    self.kill(fighter)
                if self.finished:
                    break
            if not self.finished:
                self.game.step()
        for fighter in self.fighters.values():
            if fighter.spectator:
                self.spectators.follow(fighter)
        self.noises = [n for n in self.noises if self.clock - n[0] < 2]
        self.feed = [entry for entry in self.feed if self.clock < entry[0]][-4:]
        self.publish()

    def publish(self):
        from engine.monster_facing import face_direction
        from engine.monster_ai import _set_render_flag
        # Stock MonsterAI deliberately skips these actors. Publish their real
        # aim heading for local bots, host-side guests and replicated players.
        for fighter in self.fighters.values():
            if fighter.entity is not None:
                yaw = fighter.actor.angle
                face_direction(fighter.entity, (math.sin(yaw), 0, math.cos(yaw)))
                _set_render_flag(fighter.entity, 'is_shooting',
                                 fighter.alive and self.clock < fighter.shoot_until)
        rows = sorted(self.fighters.values(), key=lambda f: (-f.frags, f.deaths, f.name))
        human = self.fighters["player"]
        leaders = self.leaders()
        # Immutable replacement: render thread never traverses live AI state.
        self.snapshot = dict(
            rows=tuple((f.name, f.frags, f.deaths, f.alive) for f in rows),
            remaining=max(0, math.ceil(self.duration - self.clock)) if self.duration else None,
            limit=self.limit, finished=self.finished,
            winner=self.result_winner if self.finished else "Draw" if len(leaders) != 1 else leaders[0].name,
            reason=self.reason, feed=tuple(text for _, text in self.feed),
            respawn=max(0, math.ceil(human.respawn_at - self.clock)) if not human.alive else 0,
            show=bool(self.settings.properties.get("show_hud", True)),
            mode=self.game.mode, team_scores=tuple(self.game.scores.items()),
            capture_limit=self.game.capture_limit,
            teams=tuple((f.name, f.team, f.captures, f.carrying) for f in rows),
            flags=tuple((flag.team, "carried" if flag.carrier else "dropped" if flag.dropped else "home")
                        for flag in self.game.flags.values()) if self.game.ctf else (),
            network_client=self.network is not None and self.network.is_client,
            spectating=human.spectator,
            watching=self.fighters[human.spectator_target].name if human.spectator_target in self.fighters else None,
            spectators=tuple(f.name for f in rows if f.spectator),
            watchable=tuple((f.key, f.name, f.team, f.alive, f.spectator) for f in rows),
        )

    def stop(self):
        if not self._started:
            return
        self._started = False
        self.spectators.stop()
        if self.network is not None:
            self.network.close()
            self.network = None
        self.game.stop()
        for obj, name, original, wrapped in reversed(self._hooks):
            if getattr(obj, name) is wrapped:
                setattr(obj, name, original)
        self._hooks.clear()
        for entity, pos, properties in self._authored:
            entity.pos = list(pos)
            entity.properties.clear()
            entity.properties.update(copy.deepcopy(properties))
        scene = self.logic.editor_state.things
        for entity in self._spawned:
            if entity in scene:
                scene.remove(entity)
        for entity, index in reversed(self._removed):
            if entity not in scene:
                scene.insert(index, entity)
        if (self._spawned or self._removed) and self.logic.session_runtime.play_mode:
            self.logic.world_runtime.build_entity_caches()
        rt = self.logic.player_runtime
        for key, value in self._player_state.items():
            setattr(rt, key, value)
        combat = self.logic.combat_runtime
        combat.weapons, combat.active_weapon, combat.player_ammo = self._loadout
        if rt.player is not None:
            import glm
            pos, angle, pitch = self._player_pose
            rt.player.pos, rt.player.angle, rt.player.pitch = glm.vec3(pos), angle, pitch
            rt.player.velocity = glm.vec3(0)
        self.snapshot = {}
