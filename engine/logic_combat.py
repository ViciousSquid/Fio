"""Combat and projectile runtime delegated from LogicThread.

Owns player hitscan shooting, monster projectile simulation, bullet marks, and
player-noise events. LogicThread remains the tick-order orchestrator.
"""

from __future__ import annotations

import math
import time

import glm
import numpy as np

from .constants import is_solid_world_brush, is_water_brush
from .change_journal import touch
from .items import DEFAULT_REGISTRY, ITEM_IDS
from .projectile_table import ProjectileStore
from .monster_constants import MONSTER_PROJECTILE_MAX_DIST

try:
    from editor.things import Monster as MonsterThing
except ImportError:
    MonsterThing = None

try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")


#: ``owner_id`` of a projectile the player fired. Monster projectiles carry
#: the firing monster's ``id()``, which is never 0.
PLAYER_PROJECTILE_OWNER = 0

#: Shared, read-only "no projectiles" array for the published frame.
NO_PROJECTILES = np.empty((0, 3), dtype=np.float32)
NO_PROJECTILES.flags.writeable = False


class LogicCombat:
    """Runtime for weapons, projectiles, bullet marks, and player noise.

    The player's weapons are the item slots (:mod:`engine.items`): ``weapons``
    holds the ids of the weapon items the player has picked up, in the order of
    the slots, and ``active_weapon`` the one in hand. How a weapon fires is its
    compiled :class:`~engine.items.WeaponSpec`, resolved through ``items`` --
    the session's :class:`~engine.items.ItemRegistry`.
    """

    def __init__(self, logic):
        self.logic = logic
        self._monster_projectiles = ProjectileStore()
        self.bullet_marks = []
        self.projectile_positions = NO_PROJECTILES
        self.items = DEFAULT_REGISTRY
        self.active_weapon = None
        self.weapons = set()
        self.player_ammo = 0
        self._last_player_shot_time = float('-inf')
        self.BULLET_FADE_TIME = 20.0
        self.muzzle_flash_active = False
        #: Bumped each time a slot key takes a weapon the player has in hand.
        self.weapon_switch_serial = 0
        self._gunfire_events = []

    # -- the player's weapons -------------------------------------------------

    def reset_weapons(self):
        """Unarmed, with no ammunition: how every play session starts."""
        self.active_weapon = None
        self.weapons = set()
        self.player_ammo = 0
        self._last_player_shot_time = float('-inf')

    def give_weapon(self, item):
        """The player picks up weapon *item* and takes it in hand.

        A weapon's ammunition is granted the first time the player gets it;
        picking it up again only takes it in hand.
        """
        if item.id not in self.weapons:
            self.weapons.add(item.id)
            if item.weapon.ammo:
                self.player_ammo = max(int(self.player_ammo), item.weapon.ammo)
        self.active_weapon = item.id

    def select_slot(self, slot):
        """Take the weapon in slot *slot* (1-4) in hand, if the player has it."""
        if 1 <= slot <= len(ITEM_IDS):
            item_id = ITEM_IDS[slot - 1]
            if item_id in self.weapons:
                self.active_weapon = item_id
                self.weapon_switch_serial += 1

    def held_weapon(self):
        """The :class:`~engine.items.WeaponSpec` in hand, or None."""
        item = self.items.resolve(self.active_weapon) if self.active_weapon else None
        return item.weapon if item is not None else None

    def shot_ready(self, now):
        """Whether a shot fired at *now* would go off."""
        weapon = self.held_weapon()
        return (weapon is not None and weapon.fires
                and now - self._last_player_shot_time >= weapon.cooldown
                and int(self.player_ammo) >= weapon.ammo_per_shot)

    def _handle_shooting(self):
        """Fire the weapon in hand, as its definition says.

        Authoritative on the logic thread, so a burst of UI clicks can never
        beat a cooldown or spend ammunition twice. A weapon whose mode is
        ``none`` is held and shown but never fires: no flash, no sound, no
        noise.
        """
        logic = self.logic
        if not logic.player_runtime.player:
            return
        weapon = self.held_weapon()
        if weapon is None or not weapon.fires:
            return
        now = time.perf_counter()
        if not self.shot_ready(now):
            return
        self.player_ammo = int(self.player_ammo) - weapon.ammo_per_shot
        self._last_player_shot_time = now

        self.muzzle_flash_active = True
        if weapon.sound:
            logic.game_state.queue_sound({"file": weapon.sound, "volume": 1.0})
        logic._plugin_emit("player_shoot", weapon=self.active_weapon)
        ray_origin, ray_dir = self._aim()
        if weapon.mode == 'projectile':
            self._fire_projectile(weapon, ray_origin, ray_dir)
            return
        any_miss = False
        for pellet in range(weapon.pellets):
            direction = self._scatter(ray_dir, weapon.spread, pellet)
            if not self._fire_ray(weapon, ray_origin, direction):
                any_miss = True
        # Monsters hear a shot that hit nothing; one that hits is the fight.
        if any_miss and weapon.noise > 0.0:
            self._emit_noise_event(
                [ray_origin.x, ray_origin.y, ray_origin.z],
                source='gunfire', loudness=weapon.noise)

    def _aim(self):
        """The ray a shot leaves along: from the eye, along the view."""
        logic = self.logic
        yaw_rad = logic.player_runtime.player.angle
        if logic.camera.is_overhead():
            # Top-down aiming is planar: the player rotates to face a target and
            # fires along that ground heading. The overhead camera and sprite
            # both ignore pitch, so there is no way to aim vertically — folding
            # pitch into the ray would just tilt shots into the sky or floor and
            # make monsters (which stand on the ground plane) nearly unhittable.
            # Keep the ray horizontal at eye height so it can actually connect.
            dir_x = math.sin(yaw_rad)
            dir_y = 0.0
            dir_z = math.cos(yaw_rad)
        else:
            pitch_rad = logic.player_runtime.player.pitch
            dir_x = math.sin(yaw_rad) * math.cos(pitch_rad)
            dir_y = math.sin(pitch_rad)
            dir_z = math.cos(yaw_rad) * math.cos(pitch_rad)
        ray_origin = glm.vec3(logic.player_runtime.player.pos.x,
                              logic.player_runtime.player.pos.y + logic.player_runtime.player.camera_height,
                              logic.player_runtime.player.pos.z)
        return ray_origin, glm.normalize(glm.vec3(dir_x, dir_y, dir_z))

    @staticmethod
    def _scatter(direction, spread, pellet):
        """*direction* turned by up to *spread* degrees; unchanged at 0.

        Deterministic per pellet index (a fixed sunflower pattern across the
        cone), so a shotgun's pattern is the same every shot.
        """
        if spread <= 0.0:
            return direction
        golden = 2.399963229728653
        radius = math.radians(spread) * math.sqrt((pellet + 0.5) / 8.0)
        angle = pellet * golden
        up = glm.vec3(0.0, 1.0, 0.0)
        right = glm.cross(direction, up)
        if glm.length(right) < 1e-6:
            right = glm.vec3(1.0, 0.0, 0.0)
        right = glm.normalize(right)
        up = glm.normalize(glm.cross(right, direction))
        offset = right * (math.cos(angle) * radius) + up * (math.sin(angle) * radius)
        return glm.normalize(direction + offset)

    def _fire_projectile(self, weapon, origin, direction):
        """One projectile into the shared projectile store, owned by the player."""
        speed = weapon.projectile_speed
        self._add_monster_projectile(
            (origin.x, origin.y, origin.z),
            (direction.x * speed, direction.y * speed, direction.z * speed),
            PLAYER_PROJECTILE_OWNER, weapon.damage,
            min(weapon.range, MONSTER_PROJECTILE_MAX_DIST) / speed)
        self._emit_noise_event([origin.x, origin.y, origin.z],
                               source='gunfire', loudness=weapon.noise)

    def _fire_ray(self, weapon, ray_origin, ray_dir):
        """Trace one hitscan or melee ray; True if it hit a monster."""
        logic = self.logic
        reach = weapon.range
        closest_brush_hit = None
        closest_brush_dist = float('inf')
        collision_brushes = logic.collision_runtime._collision_brushes_cache
        for brush in collision_brushes:
            if (brush.get('is_trigger') or brush.get('hidden') or
                is_water_brush(brush) or brush.get('is_fog')):
                continue
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            min_b = pos - size * 0.5
            max_b = pos + size * 0.5
            hit, dist = self.intersect_ray_aabb(ray_origin, ray_dir, min_b, max_b, reach)
            if hit and dist < closest_brush_dist:
                closest_brush_dist = dist
                closest_brush_hit = ray_origin + ray_dir * dist
        
        # Raycast against monsters — acquire lock for consistent positions
        closest_monster = None
        closest_monster_dist = float('inf')
        
        with logic.session_runtime.monster_lock:
            for thing in logic.editor_state.things:
                if not isinstance(thing, MonsterThing):
                    continue
                if thing.properties.get('dead', False) or thing.properties.get('hidden', False):
                    continue
                sprite_width = float(thing.properties.get('sprite_width', 64.0))
                sprite_height = float(thing.properties.get('sprite_height', 128.0))
                # Use a wider, more forgiving hit box for better gameplay feel
                # Width matters more than height for shooting comfort
                radius = max(sprite_width * 0.75, sprite_height * 0.4, 48.0)
                center = glm.vec3(thing.pos[0], thing.pos[1] + sprite_height * 0.45, thing.pos[2])
                oc = ray_origin - center
                a = glm.dot(ray_dir, ray_dir)
                b = 2.0 * glm.dot(oc, ray_dir)
                c = glm.dot(oc, oc) - radius * radius
                disc = b * b - 4 * a * c
                if disc >= 0:
                    t = (-b - math.sqrt(disc)) / (2.0 * a)
                    if 0 <= t <= reach and t < closest_monster_dist:
                        if t < closest_brush_dist:
                            closest_monster_dist = t
                            closest_monster = thing

            if closest_monster is not None:
                damage = weapon.damage
                health_raw = closest_monster.properties.get('health', 100)
                try:
                    health = int(health_raw)
                except (ValueError, TypeError):
                    health = 100
                new_health = health - damage
                closest_monster.properties['health'] = new_health
                debug_log("MonsterAI", f"Monster {closest_monster.properties.get('name')} health: {health} -> {new_health} (weapon={self.active_weapon}, dmg={damage})")
                logic.game_state.queue_sound({
                    'file': 'hit.wav',
                    'volume': 1.0,
                    'entity_id': id(closest_monster)
                })
                if logic.io_manager:
                    logic.io_manager.fire_output(closest_monster, 'OnDamaged')
                if new_health <= 0:
                    closest_monster.properties['dead'] = True
                    closest_monster.properties.pop('is_shooting', None)
                    touch(closest_monster)
                    if logic.io_manager:
                        logic.io_manager.fire_output(closest_monster, 'OnDeath')
                    if logic.monster_ai.monster_debug_active:
                        name = closest_monster.properties.get('name', '?')
                        debug_log("MonsterAI",
                            f'<a href="filter:{name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{name}</a> '
                            f'<span style="color: #B71C1C; font-weight: bold;">DIED</span> (shot by player)')
                return True

        # A melee strike leaves no mark on the wall it meets.
        if closest_brush_hit is not None and weapon.mode == 'hitscan':
            self.bullet_marks.append({
                'pos': closest_brush_hit,
                'time': time.perf_counter()
            })
        return False


    def intersect_ray_aabb(self, origin, direction, box_min, box_max, max_dist=10000.0):
        t_min = 0.0
        t_max = max_dist
        for i in range(3):
            if abs(direction[i]) < 1e-6:
                if origin[i] < box_min[i] or origin[i] > box_max[i]:
                    return False, 0
            else:
                inv_d = 1.0 / direction[i]
                t1 = (box_min[i] - origin[i]) * inv_d
                t2 = (box_max[i] - origin[i]) * inv_d
                t_near = min(t1, t2)
                t_far = max(t1, t2)
                t_min = max(t_min, t_near)
                t_max = min(t_max, t_far)
                if t_min > t_max:
                    return False, 0
        return True, t_min


    def _update_bullet_marks(self):
        logic = self.logic
        current_time = time.perf_counter()
        self.bullet_marks = [
            m for m in self.bullet_marks 
            if (current_time - m['time']) < self.BULLET_FADE_TIME
        ]

    # =========================================================================
    # MONSTER PROJECTILES (flying monster ranged attacks)
    # =========================================================================

    def _add_monster_projectile(self, pos, vel, owner_id, damage, lifetime):
        """Add one projectile directly to the dense numeric store."""
        logic = self.logic
        with logic.session_runtime.monster_lock:
            return self._monster_projectiles.add(
                pos, vel, owner_id, damage, lifetime
            )


    #: Monster hit sphere for projectiles: centred 64 units above the
    #: monster's origin, radius 64.
    PROJECTILE_MONSTER_LIFT = 64.0
    PROJECTILE_MONSTER_RADIUS = 64.0
    #: Player hit sphere for projectiles.
    PROJECTILE_PLAYER_RADIUS = 32.0


    def _projectile_monster_candidates(self, pos32, owners):
        """``(projectile, monster row)`` pairs inside a monster's hit sphere.

        The monsters are hashed into cells twice the hit radius wide, so a
        projectile's candidates are the monsters filed in the 3x3 cells round
        it; the exact test is the float32 distance the ``glm`` walk used. The
        owner and the owner's team are excluded here; dead and hidden are
        judged live when a hit is applied, because a hit earlier in the pass
        can kill a monster this list still holds.
        """
        logic = self.logic
        monsters = logic.world_runtime.monster_things
        empty = np.empty(0, dtype=np.int64)
        if not monsters or not len(pos32):
            return monsters, empty, empty
        count = len(monsters)
        centres = np.empty((count, 3), dtype=np.float32)
        centres[:] = [m.pos for m in monsters]
        centres[:, 1] += np.float32(self.PROJECTILE_MONSTER_LIFT)
        codes = {}
        team = np.fromiter(
            (codes.setdefault(m.properties.get('team', ''), len(codes))
             if m.properties.get('team', '') else -1 for m in monsters),
            dtype=np.int64, count=count)
        row_of = {id(m): row for row, m in enumerate(monsters)}

        cell = 2.0 * self.PROJECTILE_MONSTER_RADIUS
        mcx = np.floor(centres[:, 0] / cell).astype(np.int64)
        mcz = np.floor(centres[:, 2] / cell).astype(np.int64)
        mkey = (mcx + (1 << 30)) * (1 << 31) + (mcz + (1 << 30))
        order = np.argsort(mkey, kind='stable')
        keys = mkey[order]
        pcx = np.floor(pos32[:, 0] / cell).astype(np.int64)
        pcz = np.floor(pos32[:, 2] / cell).astype(np.int64)
        query_parts, row_parts = [], []
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                qkey = (pcx + dx + (1 << 30)) * (1 << 31) + (pcz + dz + (1 << 30))
                lo = np.searchsorted(keys, qkey, side='left')
                hi = np.searchsorted(keys, qkey, side='right')
                counts = hi - lo
                total = int(counts.sum())
                if not total:
                    continue
                query = np.repeat(np.arange(len(qkey)), counts)
                within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
                query_parts.append(query)
                row_parts.append(order[np.repeat(lo, counts) + within])
        if not query_parts:
            return monsters, empty, empty
        query = np.concatenate(query_parts)
        row = np.concatenate(row_parts)
        d = centres[row] - pos32[query]
        near = (d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1] + d[:, 2] * d[:, 2]
                < np.float32(self.PROJECTILE_MONSTER_RADIUS) ** 2)
        owner_row = np.array([row_of.get(o, -1) for o in owners], dtype=np.int64)
        owner = owner_row[query]
        near &= row != owner
        owner_team = np.where(owner >= 0, team[np.maximum(owner, 0)], -1)
        near &= ~((owner_team >= 0) & (team[row] == owner_team))
        query, row = query[near], row[near]
        # First in things order within each projectile.
        order = np.lexsort((row, query))
        return monsters, query[order], row[order]


    def _projectile_wall_candidates(self, pos32):
        """``(projectile, brush)`` pairs whose box holds the projectile's point.

        The brushes filed in the projectile's cell -- what ``get_nearby_brushes``
        returned -- tested inclusively against the same float64 boxes. Whether
        each is solid is asked live, of the few that contain a point.
        """
        logic = self.logic
        grid = logic.session_runtime.spatial_grid
        rows = grid._cell_rows
        rows.refresh_movers()
        cs = grid.cell_size
        x = pos32[:, 0].astype(np.float64)
        y = pos32[:, 1].astype(np.float64)
        z = pos32[:, 2].astype(np.float64)
        query, row = rows.pairs(np.floor(x / cs), np.floor(z / cs))
        if not len(query):
            return {}
        lo = rows.lo[row]
        hi = rows.hi[row]
        inside = ((lo[:, 0] <= x[query]) & (x[query] <= hi[:, 0])
                  & (lo[:, 1] <= y[query]) & (y[query] <= hi[:, 1])
                  & (lo[:, 2] <= z[query]) & (z[query] <= hi[:, 2]))
        hits = {}
        for q, r in zip(query[inside].tolist(), row[inside].tolist()):
            hits.setdefault(q, []).append(r)
        return hits

    # Below this count, the Python scalar path avoids allocating the batch
    # query/candidate arrays. Keep the threshold isolated so profiling can tune
    # the crossover without changing either implementation.
    PROJECTILE_DENSE_THRESHOLD = 100


    def _publish_projectile_render_snapshot(self):
        """Snapshot live projectile positions for the render buffer.

        Rendering can run a frame without a whole simulation tick. Keep the
        published positions derived from the authoritative ProjectileStore so
        those frames cannot accidentally publish an empty/stale projectile set.
        The copy is intentional: the renderer never aliases mutable simulation
        storage.
        """
        logic = self.logic
        with logic.session_runtime.monster_lock:
            projectiles = self._monster_projectiles
            count = len(projectiles)
            if count:
                self.projectile_positions = projectiles.pos[:count].astype(
                    np.float32, copy=True
                )
            else:
                self.projectile_positions = NO_PROJECTILES
            return self.projectile_positions


    def _update_monster_projectiles(self, delta: float):
        """Use a scalar path for small swarms and the dense path for large ones."""
        logic = self.logic

        with logic.session_runtime.monster_lock:
            projectiles = self._monster_projectiles
            if not projectiles:
                self.projectile_positions = NO_PROJECTILES
                return

            if len(projectiles) < self.PROJECTILE_DENSE_THRESHOLD:
                self._update_monster_projectiles_scalar(projectiles, delta)
            else:
                self._update_monster_projectiles_dense(projectiles, delta)


    def _update_monster_projectiles_scalar(self, projectiles, delta: float):
        """Advance a small projectile set row-by-row without batch allocations."""
        logic = self.logic

        count = len(projectiles)
        has_portals = bool(logic.portal_runtime.portal_things)
        collision_brushes = logic.collision_runtime._collision_brushes_cache
        survivors = []

        player = logic.player_runtime.player
        player_can_be_hit = (
            player is not None
            and not logic.player_runtime.god_mode
            and not logic.player_runtime.player_dead
        )
        if player is not None:
            player_pos = player.pos
            player_radius_sq = float(self.PROJECTILE_PLAYER_RADIUS) ** 2
        else:
            player_pos = None
            player_radius_sq = 0.0

        radius_sq = float(self.PROJECTILE_MONSTER_RADIUS) ** 2
        lift = float(self.PROJECTILE_MONSTER_LIFT)

        for i in range(count):
            pos = projectiles.pos[i]
            vel = projectiles.vel[i]
            if has_portals:
                prev = (float(pos[0]), float(pos[1]), float(pos[2]))
            else:
                prev = None

            pos[0] += vel[0] * delta
            pos[1] += vel[1] * delta
            pos[2] += vel[2] * delta

            if prev is not None:
                logic.portal_runtime.transit_projectile_through_portals(projectiles, i, prev)

            vx = float(projectiles.vel[i, 0])
            vy = float(projectiles.vel[i, 1])
            vz = float(projectiles.vel[i, 2])
            speed = math.sqrt(vx * vx + vy * vy + vz * vz)

            distance = float(projectiles.distance[i]) + speed * delta
            lifetime = float(projectiles.lifetime[i]) - delta
            projectiles.distance[i] = distance
            projectiles.lifetime[i] = lifetime

            if distance >= MONSTER_PROJECTILE_MAX_DIST or lifetime <= 0.0:
                continue

            px = float(projectiles.pos[i, 0])
            py = float(projectiles.pos[i, 1])
            pz = float(projectiles.pos[i, 2])
            owner_id = int(projectiles.owner_id[i])

            if player_can_be_hit and owner_id != PLAYER_PROJECTILE_OWNER:
                dx = px - float(player_pos[0])
                dy = py - float(player_pos[1])
                dz = pz - float(player_pos[2])
                if dx * dx + dy * dy + dz * dz < player_radius_sq:
                    damage = float(projectiles.damage[i])
                    logic.trigger_runtime._apply_player_damage(damage)
                    if logic.monster_ai.monster_debug_active:
                        debug_log(
                            "MonsterAI",
                            f"Projectile hit player for {damage} dmg",
                        )
                    continue

            owner = logic.world_runtime.monster_by_id.get(owner_id)
            owner_team = (
                owner.properties.get('team', '')
                if owner is not None else None
            )

            hit_monster = None
            for candidate in logic.world_runtime.monster_things:
                if id(candidate) == owner_id:
                    continue
                cp = candidate.properties
                if cp.get('dead', False) or cp.get('hidden', False):
                    continue
                candidate_team = cp.get('team', '')
                if owner_team and candidate_team and owner_team == candidate_team:
                    continue

                centre_x = float(candidate.pos[0])
                centre_y = float(candidate.pos[1]) + lift
                centre_z = float(candidate.pos[2])
                dx = px - centre_x
                dy = py - centre_y
                dz = pz - centre_z
                if dx * dx + dy * dy + dz * dz < radius_sq:
                    hit_monster = candidate
                    break

            if hit_monster is not None:
                damage = float(projectiles.damage[i])
                logic.monster_ai._apply_monster_damage(
                    hit_monster, damage, attacker=None
                )
                if logic.monster_ai.monster_debug_active:
                    name = hit_monster.properties.get('name', '?')
                    debug_log(
                        "MonsterAI",
                        f"Projectile hit {name} for {damage} dmg",
                    )
                continue

            hit_wall = False
            for brush in collision_brushes:
                if not is_solid_world_brush(brush):
                    continue
                bp = brush['pos']
                bs = brush['size']
                if (
                    bp[0] - bs[0] * 0.5 <= px <= bp[0] + bs[0] * 0.5
                    and bp[1] - bs[1] * 0.5 <= py <= bp[1] + bs[1] * 0.5
                    and bp[2] - bs[2] * 0.5 <= pz <= bp[2] + bs[2] * 0.5
                ):
                    hit_wall = True
                    break
            if hit_wall:
                continue

            survivors.append(i)

        if survivors:
            keep = np.asarray(survivors, dtype=np.intp)
            projectiles.compact(
                keep,
                projectiles.pos[:count],
                projectiles.vel[:count],
                projectiles.lifetime[:count],
                projectiles.distance[:count],
            )
            live_count = len(survivors)
            # Publication makes the immutable render snapshot copy once.
            # Keep the simulation-side value as a view until then.
            self.projectile_positions = projectiles.pos[:live_count]
        else:
            projectiles.clear()
            self.projectile_positions = NO_PROJECTILES


    def _update_monster_projectiles_dense(self, projectiles, delta: float):
        """Advance monster projectiles through the batched dense numeric path."""
        logic = self.logic

        count = len(projectiles)
        pos = projectiles.pos[:count]
        vel = projectiles.vel[:count]
        prev = None

        # Portal transit needs the previous segment endpoint. The common
        # no-portal path stays entirely in the persistent arrays.
        if logic.portal_runtime.portal_things:
            prev = pos.copy()
        pos += vel * delta
        if prev is not None:
            for i in range(count):
                logic.portal_runtime.transit_projectile_through_portals(
                    projectiles, i, tuple(prev[i]))

        speed = np.sqrt(
            vel[:, 0] * vel[:, 0]
            + vel[:, 1] * vel[:, 1]
            + vel[:, 2] * vel[:, 2]
        )
        travelled = projectiles.distance[:count] + speed * delta
        lifetime = projectiles.lifetime[:count] - delta
        live = (
            (travelled < MONSTER_PROJECTILE_MAX_DIST)
            & (lifetime > 0.0)
        )
        # ProjectileStore positions are authoritative float32 storage, so do not
        # copy the array merely to restate its dtype on every dense tick.
        pos32 = pos

        # The player's hit sphere, in the float32 glm.distance used previously.
        player_hit = np.zeros(count, dtype=bool)
        if logic.player_runtime.player is not None:
            pp = logic.player_runtime.player.pos
            player32 = np.array((pp[0], pp[1], pp[2]), dtype=np.float32)
            d = pos32 - player32
            player_hit = (
                np.sqrt(
                    d[:, 0] * d[:, 0]
                    + d[:, 1] * d[:, 1]
                    + d[:, 2] * d[:, 2]
                )
                < np.float32(self.PROJECTILE_PLAYER_RADIUS)
            ) & live & (projectiles.owner_id[:count] != PLAYER_PROJECTILE_OWNER)

        grid = logic.session_runtime.spatial_grid
        all_collision_brushes = logic.collision_runtime._collision_brushes_cache
        keep = live.copy()

        live_rows = np.flatnonzero(live)
        owners = projectiles.owner_id[live_rows]
        monsters, mq, mrow = self._projectile_monster_candidates(
            pos32[live_rows], owners
        )
        mq = live_rows[mq] if len(mq) else mq
        monster_hits = {}
        for q, r in zip(mq.tolist(), mrow.tolist()):
            monster_hits.setdefault(q, []).append(r)

        if grid is not None:
            wall_hits = self._projectile_wall_candidates(pos32[live_rows])
            wall_hits = {
                int(live_rows[q]): rows
                for q, rows in wall_hits.items()
            }
            wall_brushes = grid._cell_rows
        else:
            wall_hits = None

        for i in live_rows.tolist():
            if (
                player_hit[i]
                and logic.player_runtime.player
                and not logic.player_runtime.god_mode
                and not logic.player_runtime.player_dead
            ):
                damage = float(projectiles.damage[i])
                logic.trigger_runtime._apply_player_damage(damage)
                if logic.monster_ai.monster_debug_active:
                    debug_log(
                        "MonsterAI",
                        f"Projectile hit player for {damage} dmg",
                    )
                keep[i] = False
                continue

            hit_monster = None
            for r in monster_hits.get(i, ()):
                candidate = monsters[r]
                cp = candidate.properties
                if not (cp.get('dead', False) or cp.get('hidden', False)):
                    hit_monster = candidate
                    break
            if hit_monster is not None:
                damage = float(projectiles.damage[i])
                logic.monster_ai._apply_monster_damage(
                    hit_monster, damage, attacker=None
                )
                if logic.monster_ai.monster_debug_active:
                    name = hit_monster.properties.get('name', '?')
                    debug_log(
                        "MonsterAI",
                        f"Projectile hit {name} for {damage} dmg",
                    )
                keep[i] = False
                continue

            if wall_hits is not None:
                hit_wall = any(
                    is_solid_world_brush(wall_brushes.brushes[r])
                    for r in wall_hits.get(i, ())
                )
            else:
                x, y, z = pos32[i]
                hit_wall = False
                for brush in all_collision_brushes:
                    if not is_solid_world_brush(brush):
                        continue
                    bp = brush['pos']
                    bs = brush['size']
                    if (
                        bp[0] - bs[0] * 0.5 <= x <= bp[0] + bs[0] * 0.5
                        and bp[1] - bs[1] * 0.5 <= y <= bp[1] + bs[1] * 0.5
                        and bp[2] - bs[2] * 0.5 <= z <= bp[2] + bs[2] * 0.5
                    ):
                        hit_wall = True
                        break
            if hit_wall:
                keep[i] = False

        survivors = np.flatnonzero(keep)
        projectiles.compact(survivors, pos, vel, lifetime, travelled)

        # Published as an independent snapshot so the renderer can keep
        # consuming its frame even while the next logic tick mutates the store.
        # Publication makes the immutable render snapshot copy once.
        # The compacted store is authoritative until that boundary.
        self.projectile_positions = (
            projectiles.pos[:len(survivors)]
            if len(survivors) else NO_PROJECTILES
        )
    

    # =========================================================================
    # GUNFIRE SOUND EVENTS (for AI hearing)
    # =========================================================================


    def _emit_noise_event(self, pos, source: str, loudness: float = 1.0):
        """Record an audible player action so hearing monsters can react.

        Stored in the shared player-noise list (self._gunfire_events); every
        event carries a position, timestamp, a source tag and a loudness
        multiplier that scales how far it can be heard. Used by the monster
        AI both to wake sleeping monsters and to steer awake ones toward the
        source (see MonsterAI._hears_noise / _investigate_sounds).
        """
        logic = self.logic
        self._gunfire_events.append({
            'pos': [float(pos[0]), float(pos[1]), float(pos[2])],
            'time': time.perf_counter(),
            'source': source,
            'loudness': float(loudness),
        })
        logic._plugin_emit("noise", pos=[float(pos[0]), float(pos[1]), float(pos[2])],
                          source=source, loudness=float(loudness))


    def prune_noise_events(self, max_age: float = 3.0):
        """Discard player-noise events older than *max_age* seconds."""
        current_time = time.perf_counter()
        self._gunfire_events = [
            event for event in self._gunfire_events
            if (current_time - event['time']) < max_age
        ]

    def get_recent_noise_events(self, max_age: float = 3.0) -> list:
        logic = self.logic
        current_time = time.perf_counter()
        return [
            e for e in self._gunfire_events
            if (current_time - e['time']) < max_age
        ]

    # =========================================================================
    # FRUSTUM CULLING
    # =========================================================================

