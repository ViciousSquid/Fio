"""Runtime interaction domain for the unified Prop primitive.

Physics simulation lives in engine.physics. PropSession owns all Prop
interaction state: carrying, collecting, dropping, collection respawns,
and the Prop spatial index.
"""
from __future__ import annotations

import math

from .spatial import CellIndex, cell_of_point


def _vec(p):
    return float(p[0]), float(p[1]), float(p[2])


def _forward(player):
    return (math.sin(player.angle) * math.cos(player.pitch),
            math.sin(player.pitch),
            math.cos(player.angle) * math.cos(player.pitch))


def _length(v):
    return math.sqrt(sum(x * x for x in v))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class PropSession:
    """Authoritative runtime registry and interaction state for Props.

    PropSession owns the Prop domain. The authoritative world data remains
    the Thing list; this registry and its spatial index are derived from it.
    No separate collection entity system exists.
    """

    DEFAULT_CARRY_REACH = 110.0
    DEFAULT_COLLECT_USE_REACH = 80.0
    DEFAULT_COLLECT_WALK_REACH = 32.0
    DROP_GRAVITY = 900.0
    DROP_TERMINAL_VELOCITY = 2400.0
    SPRITE_CAMERA_FACING = -10000.0
    RESPAWN_FADE_DURATION = 2.0

    def __init__(self, logic):
        self.logic = logic
        self.props = []
        self.held = None
        self.collected_ids = set()
        self.respawn_timers = {}
        self.respawn_fades = {}
        self._by_id = {}
        self._cells = CellIndex()
        self._filed = {}
        self._max_reach = self.DEFAULT_CARRY_REACH
        self._falling = {}

    @staticmethod
    def is_prop(thing):
        return getattr(thing, "properties", {}).get("type") == "prop"

    @property
    def physics(self):
        return getattr(self.logic, "_physics_world", None)

    # -- registry ---------------------------------------------------------

    def rebuild(self, things=None):
        """Re-derive the Prop registry from the authoritative Thing list."""
        if things is None:
            things = self.logic.things
        current = [t for t in things if self.is_prop(t)]
        current_ids = {id(t) for t in current}

        for prop in self.props:
            if id(prop) not in current_ids:
                self._release(prop, restore_home=False)

        known = self._by_id
        for prop in current:
            if id(prop) not in known:
                self._adopt(prop)

        self.props = current
        self._by_id = {id(t): t for t in current}
        self.collected_ids = {
            id(t) for t in current
            if t.properties.get("collect_collected", False)
        }
        if self.held is not None and id(self.held) not in current_ids:
            self.held = None

        self._recalculate_max_reach()

    def by_id(self, entity_id):
        return self._by_id.get(entity_id)

    def _adopt(self, prop):
        prop.properties["_prop_home_pos"] = list(prop.pos)
        prop.properties.pop("_drop_requested", None)
        self._file(prop)
        if self.physics is not None:
            self.physics.set_rest_callback(prop, self._on_rest)
            self.physics.set_kinematic(prop, False)

    def _release(self, prop, restore_home=True):
        home = prop.properties.pop("_prop_home_pos", None)
        prop.properties.pop("_drop_requested", None)
        self._unfile(prop)
        if self.physics is not None:
            self.physics.set_rest_callback(prop, None)
            self.physics.set_kinematic(prop, False)
        if restore_home and home is not None:
            prop.pos = home

    # -- spatial membership -----------------------------------------------

    def _interaction_reach(self, prop):
        p = prop.properties
        reach = 0.0

        if p.get("carry_enabled", True):
            try:
                reach = max(reach, float(
                    p.get("carry_reach", self.DEFAULT_CARRY_REACH)))
            except (TypeError, ValueError):
                reach = max(reach, self.DEFAULT_CARRY_REACH)

        if p.get("collect_enabled", False):
            activation = p.get("collect_activation", "walk_over")
            reach = max(
                reach,
                self.DEFAULT_COLLECT_USE_REACH
                if activation == "use"
                else self.DEFAULT_COLLECT_WALK_REACH,
            )

        return reach

    def _recalculate_max_reach(self):
        self._max_reach = self.DEFAULT_CARRY_REACH
        for prop in self.props:
            self._max_reach = max(self._max_reach, self._interaction_reach(prop))

    def _file(self, prop):
        coord = cell_of_point(float(prop.pos[0]), float(prop.pos[2]))
        self._cells.insert_point(prop, float(prop.pos[0]), float(prop.pos[2]))
        self._filed[id(prop)] = coord
        self._max_reach = max(self._max_reach, self._interaction_reach(prop))

    def _unfile(self, prop):
        coord = self._filed.pop(id(prop), None)
        if coord is not None:
            self._cells.remove_point(prop, coord)

    def moved(self, prop):
        previous = self._filed.get(id(prop))
        if previous is None:
            return
        x, z = float(prop.pos[0]), float(prop.pos[2])
        coord = cell_of_point(x, z)
        if coord == previous:
            return
        self._cells.remove_point(prop, previous)
        self._cells.insert_point(prop, x, z)
        self._filed[id(prop)] = coord

    def refile(self, props):
        for prop in props:
            self.moved(prop)

    def sync_physics_positions(self):
        physics = self.physics
        if physics is None:
            return
        changed = getattr(physics, "entities_that_changed_cell", None)
        if changed is None:
            return
        moved = changed()
        if moved:
            self.refile(moved)

    def props_within(self, x, z, radius):
        cells = self._cells
        found = []
        for coord in cells.cells_within(x, z, radius):
            found.extend(cells.cell(coord))
        return found

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self.held = None
        self.collected_ids.clear()
        self.respawn_timers.clear()
        self.respawn_fades.clear()
        self.rebuild()
        for prop in self.props:
            prop._respawn_fade_alpha = 1.0

    def stop(self):
        for prop in self.props:
            self._release(prop)
            prop._respawn_fade_alpha = 1.0
        self.held = None
        self.props = []
        self._by_id = {}
        self.collected_ids.clear()
        self.respawn_timers.clear()
        self.respawn_fades.clear()
        self._falling.clear()
        self._cells.clear()
        self._filed.clear()
        self._max_reach = self.DEFAULT_CARRY_REACH

    # -- I/O --------------------------------------------------------------

    def _fire(self, prop, output):
        io = getattr(self.logic, "io_manager", None)
        if io is not None:
            io.fire_output(prop, output)

    def _on_rest(self, prop):
        self._fire(prop, "OnRest")

    # -- interaction ------------------------------------------------------

    def tick(self, delta, use_pressed):
        player = getattr(self.logic, "player", None)
        if player is None:
            return

        self._update_respawns(delta)
        self._update_respawn_fades(delta)
        self._update_falling(delta)

        eye_pos = _vec(player.pos)
        eye = (
            eye_pos[0],
            eye_pos[1] + float(getattr(player, "camera_height", 40.0)),
            eye_pos[2],
        )
        forward = _forward(player)

        if self.held is not None:
            self._carry(eye, forward, use_pressed)
            return

        # Walk-over collection is passive and therefore happens before any
        # use-driven carry/collection decision.
        if self._collect_walk_over():
            return

        if use_pressed:
            # A use-activated collectible wins over carrying the same Prop.
            if self._collect_in_view(eye, forward):
                return
            self._carry_in_view(eye, forward)

    def _carry_in_view(self, eye, forward):
        candidates = self.props_within(
            eye[0], eye[2], self._max_reach)
        best = None
        best_distance = None

        for prop in candidates:
            p = prop.properties
            if p.get("disabled") or not p.get("carry_enabled", True):
                continue
            if p.get("collect_collected", False):
                continue

            dx = float(prop.pos[0]) - eye[0]
            dy = float(prop.pos[1]) - eye[1]
            dz = float(prop.pos[2]) - eye[2]
            distance = _length((dx, dy, dz))
            reach = float(p.get("carry_reach", self.DEFAULT_CARRY_REACH))
            if distance < 0.001 or distance > reach:
                continue
            if _dot(forward, (
                dx / distance, dy / distance, dz / distance
            )) < 0.86:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = prop, distance

        if best is not None:
            self.held = best
            # Lock the billboard's facing direction at pickup. The world
            # position still follows the player's view, but the sprite itself
            # no longer rotates with the camera.
            try:
                best._carry_sprite_yaw = float(getattr(getattr(self.logic, "player", None), "angle", 0.0))
            except (TypeError, ValueError):
                best._carry_sprite_yaw = 0.0
            self._falling.pop(id(best), None)
            if self.physics is not None:
                self.physics.set_kinematic(best, True)
            self._fire(best, "OnCarried")

    def _collectable(self, prop):
        p = prop.properties
        return (
            p.get("collect_enabled", False)
            and not p.get("collect_collected", False)
            and not p.get("disabled", False)
        )

    def _collect_walk_over(self):
        player = getattr(self.logic, "player", None)
        if player is None:
            return False

        player_pos = _vec(player.pos)
        for prop in self.props_within(
            player_pos[0], player_pos[2],
            self.DEFAULT_COLLECT_WALK_REACH,
        ):
            p = prop.properties
            if not self._collectable(prop):
                continue
            if p.get("collect_activation", "walk_over") != "walk_over":
                continue

            dx = float(prop.pos[0]) - player_pos[0]
            dy = float(prop.pos[1]) - player_pos[1]
            dz = float(prop.pos[2]) - player_pos[2]
            if _length((dx, dy, dz)) <= self.DEFAULT_COLLECT_WALK_REACH:
                self._collect(prop)
                return True
        return False

    def _collect_in_view(self, eye, forward):
        candidates = self.props_within(
            eye[0], eye[2], self.DEFAULT_COLLECT_USE_REACH)
        best = None
        best_distance = None

        for prop in candidates:
            p = prop.properties
            if not self._collectable(prop):
                continue
            if p.get("collect_activation", "walk_over") != "use":
                continue

            dx = float(prop.pos[0]) - eye[0]
            dy = float(prop.pos[1]) - eye[1]
            dz = float(prop.pos[2]) - eye[2]
            distance = _length((dx, dy, dz))
            if distance < 0.001 or distance > self.DEFAULT_COLLECT_USE_REACH:
                continue
            if _dot(forward, (
                dx / distance, dy / distance, dz / distance
            )) <= 0.8:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = prop, distance

        if best is None:
            return False

        collect_label = str(
            best.properties.get("collect_type", "health")
        ).replace("_", " ").title()
        self.logic.current_hud_message = f"[E] Collect {collect_label}"
        self._collect(best)
        return True

    def _collect(self, prop):
        p = prop.properties
        collect_type = p.get("collect_type", "health")
        value = p.get("collect_value", 25)

        try:
            value_num = int(value)
        except (TypeError, ValueError):
            value_num = 25

        if collect_type == "health":
            try:
                self.logic.player_health = min(
                    self.logic.player_max_health,
                    self.logic.player_health + value_num,
                )
            except (AttributeError, TypeError):
                pass
        elif collect_type == "key":
            key_name = p.get("collect_key_name", "")
            if key_name:
                self.logic.collected_keys.add(key_name)
                self.logic.current_hud_key_name = key_name
        elif collect_type == "ammo":
            try:
                current_ammo = max(0, int(
                    getattr(self.logic, "player_ammo", 0)))
            except (AttributeError, TypeError, ValueError):
                current_ammo = 0
            self.logic.player_ammo = current_ammo + max(0, value_num)
        elif collect_type == "weapon":
            weapon = p.get("collect_weapon", "gun1")
            self.logic.active_weapon = weapon
            if weapon == "gun2" and not getattr(
                    self.logic, "gun2_obtained", False):
                self.logic.gun2_obtained = True
                try:
                    current_ammo = max(
                        0, int(getattr(self.logic, "player_ammo", 0)))
                except (AttributeError, TypeError, ValueError):
                    current_ammo = 0
                self.logic.player_ammo = max(current_ammo, 8)
            self.logic.current_hud_message = f"Collected {str(weapon).upper()}"

        p["collect_collected"] = True
        self.collected_ids.add(id(prop))
        if self.physics is not None:
            self.physics.set_kinematic(prop, True)

        self._fire(prop, "OnCollected")

        emit = getattr(self.logic, "_plugin_emit", None)
        if emit is not None:
            emit(
                "prop_collected",
                prop=prop,
                collect_type=collect_type,
                value=value_num,
            )

        if p.get("collect_respawns", False):
            try:
                respawn_time = float(
                    p.get("collect_respawn_time", 20.0))
            except (TypeError, ValueError):
                respawn_time = 20.0
            self.respawn_timers[id(prop)] = {
                "remaining": max(0.0, respawn_time),
                "entity": prop,
            }

    def collect_prop(self, prop):
        """Collect *prop* through the same path as player collection."""
        if prop is None or id(prop) not in self._by_id:
            return False
        if not self._collectable(prop):
            return False
        self._collect(prop)
        return True

    def respawn_prop(self, prop):
        """Force a collected Prop back into its authored live state."""
        if prop is None or id(prop) not in self._by_id:
            return False
        pid = id(prop)
        self.respawn_timers.pop(pid, None)
        prop.properties["collect_collected"] = False
        self._start_respawn_fade(prop)
        self.collected_ids.discard(pid)
        if self.held is prop:
            self.held = None
        if self.physics is not None:
            self.physics.set_kinematic(prop, False)
        self._fire(prop, "OnRespawn")
        return True

    def _update_respawns(self, delta):
        if not self.respawn_timers:
            return

        finished = []
        for pid, state in list(self.respawn_timers.items()):
            state["remaining"] -= delta
            if state["remaining"] <= 0.0:
                finished.append(pid)

        for pid in finished:
            state = self.respawn_timers.pop(pid)
            prop = state.get("entity")
            if prop is None or id(prop) != pid:
                continue
            if id(prop) not in self._by_id:
                continue

            prop.properties["collect_collected"] = False
            self.collected_ids.discard(pid)
            self._start_respawn_fade(prop)
            if self.physics is not None:
                self.physics.set_kinematic(prop, False)
            self._fire(prop, "OnRespawn")

    def _start_respawn_fade(self, prop):
        """Make a newly respawned Prop fully transparent for a 2-second fade."""
        pid = id(prop)
        prop._respawn_fade_alpha = 0.0
        self.respawn_fades[pid] = {
            "remaining": self.RESPAWN_FADE_DURATION,
            "entity": prop,
        }

    def _update_respawn_fades(self, delta):
        if not self.respawn_fades:
            return

        finished = []
        for pid, state in list(self.respawn_fades.items()):
            prop = state.get("entity")
            if prop is None or id(prop) != pid or id(prop) not in self._by_id:
                finished.append(pid)
                continue

            state["remaining"] -= max(0.0, float(delta))
            remaining = max(0.0, state["remaining"])
            prop._respawn_fade_alpha = (
                1.0 - remaining / self.RESPAWN_FADE_DURATION
            )
            if remaining <= 0.0:
                prop._respawn_fade_alpha = 1.0
                finished.append(pid)

        for pid in finished:
            self.respawn_fades.pop(pid, None)

    def _update_falling(self, delta):
        """Advance released non-physics Props until they rest on the floor."""
        if not self._falling:
            return

        physics = self.physics
        raycast_down = getattr(
            getattr(self.logic, "_spatial_grid", None),
            "raycast_down",
            None,
        )
        if raycast_down is None and physics is not None:
            raycast_down = getattr(physics, "raycast_down", None)

        finished = []
        for pid, state in tuple(self._falling.items()):
            prop = state.get("entity")
            if prop is None or id(prop) != pid or id(prop) not in self._by_id:
                finished.append(pid)
                continue

            velocity = min(
                self.DROP_TERMINAL_VELOCITY,
                float(state.get("velocity", 0.0)) + self.DROP_GRAVITY * delta,
            )
            old_y = float(prop.pos[1])
            new_y = old_y - velocity * delta
            half_height = 16.0
            try:
                size = prop.properties.get("sprite_size", [32.0, 32.0])
                if isinstance(size, (list, tuple)) and len(size) >= 2:
                    half_height = max(1.0, float(size[1]) * 0.5)
            except (TypeError, ValueError):
                pass

            floor_y = None
            if raycast_down is not None:
                try:
                    floor_y = raycast_down(
                        float(prop.pos[0]),
                        float(prop.pos[2]),
                        start_y=max(old_y + half_height, new_y + half_height),
                    )
                except TypeError:
                    floor_y = raycast_down(
                        float(prop.pos[0]),
                        float(prop.pos[2]),
                    )

            if floor_y is not None and new_y <= float(floor_y) + half_height:
                prop.pos = [prop.pos[0], float(floor_y) + half_height, prop.pos[2]]
                self.moved(prop)
                finished.append(pid)
                io = getattr(self.logic, "io_manager", None)
                if io is not None:
                    io.fire_output(prop, "OnRest")
                continue

            prop.pos = [prop.pos[0], new_y, prop.pos[2]]
            self.moved(prop)
            state["velocity"] = velocity

        for pid in finished:
            self._falling.pop(pid, None)

    def _carry(self, eye, forward, use_pressed):
        prop = self.held
        p = prop.properties
        offset = p.get("carry_offset", [0.0, -6.0, 0.0])
        distance = float(p.get("carry_distance", 55.0))

        prop.pos = [
            eye[0] + forward[0] * distance + float(offset[0]),
            eye[1] + forward[1] * distance + float(offset[1]),
            eye[2] + forward[2] * distance + float(offset[2]),
        ]
        self.moved(prop)

        if not (use_pressed or p.pop("_drop_requested", False)):
            self.logic.current_hud_message = "[E] Carry / Drop"
            return

        interceptor = getattr(self.logic, "_prop_drop_interceptor", None)
        if interceptor is not None:
            try:
                if interceptor(prop):
                    return
            except Exception as exc:
                print(f"[PropSession] drop interceptor failed: {exc!r}")

        self.held = None
        physics_body = (
            self.physics.get_body(prop)
            if self.physics is not None
            and hasattr(self.physics, "get_body")
            else None
        )
        use_physics_drop = (
            physics_body is not None
            and bool(p.get("physics_enabled", False))
        )
        if self.physics is not None:
            self.physics.set_kinematic(prop, False)
        if use_physics_drop:
            self.physics.wake(
                prop,
                [0.0, float(p.get("drop_velocity", 0.0)), 0.0],
            )
        else:
            # A carryable Prop does not have to opt into authored physics.
            # Releasing it therefore owns a tiny vertical drop state here so
            # non-physics Props still fall to the world's floor instead of
            # becoming permanently suspended at the carry position.
            self._falling[id(prop)] = {
                "entity": prop,
                "velocity": float(p.get("drop_velocity", 0.0)),
            }
        prop._carry_sprite_yaw = None
        self._fire(prop, "OnDropped")
