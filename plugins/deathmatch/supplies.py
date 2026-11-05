# SPDX-License-Identifier: GPL-2.0-or-later
# Item utility/travel-cost goals adapt Quake III botlib/be_ai_goal.c.
# Copyright id Software; see NOTICE.md and LICENSE.
"""Bot inventory and utility-based collection of native Props."""
import math

from .navigation import distance


class BotSupplies:
    def __init__(self, session, fighter):
        self.session, self.fighter = session, fighter
        self.weapons = {"gun1"}
        self.active = "gun1"
        self.ammo = self.armor = 0
        self.keys = set()
        self.target = None
        self.route = []
        self.check_at = 0.0
        self.avoid_until = {}

    def reward(self, prop):
        p = prop.properties
        kind = p.get("collect_type")
        try:
            amount = max(0, int(p.get("collect_value", 25)))
        except (ValueError, TypeError):
            amount = 25
        if kind == "key":
            return "key", p.get("collect_key_name", ""), amount
        if kind != "item":
            return kind, None, amount
        item = self.session.logic.prop_runtime._item(prop)
        if item is None:
            return None, None, 0
        if item.weapon is not None:
            return "weapon", item, 0
        pickup = item.pickup
        if pickup.effect == "weapon":
            weapon = self.session.logic.combat_runtime.items.resolve(pickup.item_id)
            return "weapon", weapon, 0
        return pickup.effect, pickup.key_name, pickup.amount

    def utility(self, prop):
        s, f = self.session, self.fighter
        runtime = s.logic.prop_runtime
        if (not f.alive or not runtime._collectable(prop) or runtime.held is prop
                or prop.properties.get("hidden") or id(prop) not in runtime._by_id
                or self.avoid_until.get(id(prop), 0) > s.clock):
            return 0
        kind, item, amount = self.reward(prop)
        if kind == "health":
            maximum = max(1, float(f.entity.properties.get("max_health", 100)))
            missing = max(0, maximum - s.health(f))
            return (100 + 200 * missing / maximum) if missing > 0 and amount > 0 else 0
        if kind == "weapon":
            if item is None or item.weapon is None or not item.weapon.fires:
                return 0
            return 100 if item.id not in self.weapons else 0
        if kind == "ammo":
            armed = any(s.logic.combat_runtime.items.resolve(w).weapon.ammo_per_shot > 0
                        for w in self.weapons if s.logic.combat_runtime.items.resolve(w) is not None)
            return (120 if self.ammo < 4 else 50) if armed and self.ammo < 24 and amount > 0 else 0
        if kind == "armor":
            return 70 if self.armor < 100 and amount > 0 else 0
        if kind == "key":
            return 20 if item and item not in self.keys else 0
        return 0

    def grant(self, prop, kind, amount, compiled_item):
        if self.utility(prop) <= 0:
            return False
        effect, item, value = self.reward(prop)
        if effect == "health":
            maximum = int(self.fighter.entity.properties.get("max_health", 100))
            self.fighter.entity.properties["health"] = min(maximum, self.session.health(self.fighter) + value)
        elif effect == "ammo":
            self.ammo += value
        elif effect == "armor":
            self.armor = min(100, self.armor + value)
        elif effect == "key":
            self.keys.add(item)
        elif effect == "weapon":
            self.weapons.add(item.id)
            self.active = item.id
            self.ammo = max(self.ammo, item.weapon.ammo)
        else:
            return False
        return True

    def collect_nearby(self, use_allowed=True):
        s, f = self.session, self.fighter
        if not f.alive or s.finished:
            return
        runtime = s.logic.prop_runtime
        for prop in runtime.props_within(f.actor.pos.x, f.actor.pos.z, runtime.DEFAULT_COLLECT_USE_REACH):
            if self.utility(prop) <= 0:
                continue
            activation = runtime._activation(prop)
            if activation == "use" and not use_allowed:
                continue
            origin = s.eye(f) if activation == "use" else tuple(f.actor.pos)
            reach = runtime.DEFAULT_COLLECT_USE_REACH if activation == "use" else runtime.DEFAULT_COLLECT_WALK_REACH
            dist = distance(origin, prop.pos)
            if dist > reach or not s.visible_points(origin, prop.pos):
                continue
            if activation == "use" and dist > 0.001:
                a, p = f.actor.angle, f.actor.pitch
                forward = (math.sin(a) * math.cos(p), math.sin(p), math.cos(a) * math.cos(p))
                dot = sum(forward[i] * (prop.pos[i] - origin[i]) / dist for i in range(3))
                if dot <= 0.8:
                    continue
            if runtime.collect_prop(prop, grant=lambda k, v, it: self.grant(prop, k, v, it), collector=f.entity):
                if self.target is prop:
                    self.target, self.route = None, []
                break

    def goal(self, fighting=False):
        s, f = self.session, self.fighter
        self.collect_nearby()
        if self.target is not None and self.utility(self.target) <= 0:
            self.target, self.route = None, []
        if s.clock >= self.check_at:
            self.check_at = s.clock + 0.5
            candidates = []
            for prop in s.logic.prop_runtime.props_within(f.actor.pos.x, f.actor.pos.z, 1400):
                value = self.utility(prop)
                if not value or (fighting and self.reward(prop)[0] != "health"):
                    continue
                if fighting and s.health(f) > float(f.entity.properties.get("max_health", 100)) * 0.35:
                    continue
                if not s.visible_points(s.eye(f), prop.pos):
                    continue
                grid = s.logic.session_runtime.spatial_grid
                ground = grid.raycast_down(prop.pos[0], prop.pos[2], prop.pos[1] + f.actor.height)
                if ground is None:
                    continue
                point = (prop.pos[0], ground, prop.pos[2])
                route = s.nav.route(s.feet(f), point, s.clear)
                if not route:
                    continue
                cost = distance(s.feet(f), route[0]) + sum(distance(a, b) for a, b in zip(route, route[1:]))
                candidates.append((value / (cost + 100), str(prop.properties["id"]), prop, route))
            if candidates:
                _, _, self.target, self.route = max(candidates, key=lambda c: (c[0], c[1]))
        if self.target is None or (fighting and self.reward(self.target)[0] != "health"):
            return None
        # Keep the final pickup approach until collection, but honour node
        # radii and safe corner transitions on every intermediate waypoint.
        s.nav.advance(self.route, s.feet(f), s.clear, default_radius=30, keep_final=True)
        return self.route[0] if self.route else None

    def weapon(self, target_distance=400):
        registry = self.session.logic.combat_runtime.items
        candidates = []
        for name in sorted(self.weapons):
            item = registry.resolve(name)
            weapon = item.weapon if item is not None else None
            if weapon is None or not weapon.fires or self.ammo < weapon.ammo_per_shot:
                continue
            if weapon.mode == "melee" and target_distance > weapon.range:
                continue
            score = weapon.damage * weapon.pellets / max(weapon.cooldown, self.session.skill(self.fighter)[2], 0.1)
            score *= min(1, weapon.range / max(1, target_distance))
            candidates.append((score, name, weapon))
        return max(candidates)[2] if candidates else None
