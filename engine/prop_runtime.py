"""Core runtime for generic :class:`editor.things.Prop` entities.

The session deliberately owns only entities whose map type is ``prop``.  Plugin
subclasses can reuse Prop's persisted contract while retaining their own game
rules, and a plain Prop map needs no plugin to be installed or enabled.
"""
from __future__ import annotations

import math


GRAVITY = -900.0
MAX_STEP = 0.05


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
    """Pickup, carrying, drop, and sleeping physics for core Props."""

    def __init__(self, logic):
        self.logic = logic
        self.props = []
        self.held = None
        self.moving = {}

    @staticmethod
    def has_props(things):
        """Return whether a map currently contains a plain core Prop."""
        return any(getattr(t, 'properties', {}).get('type') == 'prop' for t in things)

    def start(self):
        self.props = [t for t in self.logic.things
                      if getattr(t, 'properties', {}).get('type') == 'prop']
        self.held = None
        self.moving.clear()
        self._shapes = {}
        for brush in getattr(self.logic, '_model_collision_brushes', ()):
            prop = brush.get('_prop_entity')
            if prop is None or not brush.get('_dynamic_prop'):
                continue
            pos = _vec(prop.pos)
            brush_pos = _vec(brush.get('pos', pos))
            self._shapes[id(prop)] = {
                'size': list(brush.get('size', [64.0, 64.0, 64.0])),
                'offset': (
                    brush_pos[0] - pos[0],
                    brush_pos[1] - pos[1],
                    brush_pos[2] - pos[2],
                ),
            }
        for prop in self.props:
            prop.properties['_prop_home_pos'] = list(prop.pos)
            prop.properties.pop('_physics_awake', None)
            prop.properties.pop('_drop_requested', None)

    def stop(self):
        for prop in self.props:
            home = prop.properties.pop('_prop_home_pos', None)
            prop.properties.pop('_physics_awake', None)
            prop.properties.pop('_drop_requested', None)
            if home is not None:
                prop.pos = home
        self.held = None
        self.moving.clear()

    def is_empty(self):
        """Drop stale references when props are removed from the live map."""
        live = {id(t) for t in self.logic.things
                if getattr(t, 'properties', {}).get('type') == 'prop'}
        if len(live) == len(self.props) and all(id(prop) in live for prop in self.props):
            return False
        self.props = [prop for prop in self.props if id(prop) in live]
        self.moving = {key: state for key, state in self.moving.items()
                       if id(state['prop']) in live}
        if self.held is not None and id(self.held) not in live:
            self.held = None
        return not self.props

    def _fire(self, prop, output):
        io = getattr(self.logic, 'io_manager', None)
        if io is not None:
            io.fire_output(prop, output)

    def tick(self, delta, use_pressed):
        player = getattr(self.logic, 'player', None)
        if player is not None:
            eye = (_vec(player.pos)[0], _vec(player.pos)[1] +
                   float(getattr(player, 'camera_height', 40.0)), _vec(player.pos)[2])
            forward = _forward(player)
            if self.held is not None:
                self._carry(eye, forward, use_pressed)
            elif use_pressed:
                self._pick_in_view(eye, forward)
        if player is not None:
            self._push_from_player(player)
        if self.moving:
            self._simulate(delta)

    def _shape_for(self, prop):
        return self._shapes.get(id(prop))

    def _push_from_player(self, player):
        """Push overlapping physics-enabled Props away from the player."""
        if getattr(player, 'physics_enabled', True) is False:
            return

        player_pos = _vec(player.pos)
        half = getattr(player, '_half', None)
        if half is not None:
            player_half_x = float(half.x)
            player_half_y = float(half.y)
            player_half_z = float(half.z)
        else:
            player_half_x = float(getattr(player, 'width', 50.0)) * 0.5
            player_half_y = float(getattr(player, 'height', 100.0)) * 0.5
            player_half_z = float(getattr(player, 'depth', 50.0)) * 0.5

        velocity = getattr(player, 'velocity', None)
        pvx = float(getattr(velocity, 'x', 0.0))
        pvz = float(getattr(velocity, 'z', 0.0))
        speed = math.hypot(pvx, pvz)
        if speed < 0.01:
            return

        for prop in self.props:
            if prop is self.held:
                continue
            p = prop.properties
            if p.get('disabled') or not p.get('physics_enabled', False):
                continue
            if p.get('no_collision', True):
                continue

            shape = self._shape_for(prop)
            if shape is None:
                continue

            sx, sy, sz = (float(v) * 0.5 for v in shape['size'])
            off = shape['offset']
            center = (
                float(prop.pos[0]) + off[0],
                float(prop.pos[1]) + off[1],
                float(prop.pos[2]) + off[2],
            )
            dx = center[0] - player_pos[0]
            dz = center[2] - player_pos[2]
            overlap_x = player_half_x + sx - abs(dx)
            overlap_z = player_half_z + sz - abs(dz)
            prop_min_y = center[1] - sy
            prop_max_y = center[1] + sy
            player_min_y = player_pos[1] - player_half_y
            player_max_y = player_pos[1] + player_half_y

            if (overlap_x <= 0.0 or overlap_z <= 0.0 or
                    player_max_y <= prop_min_y or player_min_y >= prop_max_y):
                continue

            state = self.moving.get(id(prop))
            if state is None:
                state = {
                    'prop': prop,
                    'velocity': 0.0,
                    'velocity_x': 0.0,
                    'velocity_z': 0.0,
                }
                self.moving[id(prop)] = state

            mass = max(float(p.get('mass', 1.0)), 0.01)
            if overlap_x <= overlap_z:
                direction = 1.0 if dx >= 0.0 else -1.0
                prop.pos[0] += direction * (overlap_x + 0.5)
                push_speed = abs(pvx) / mass
                state['velocity_x'] = direction * max(
                    abs(state.get('velocity_x', 0.0)), push_speed * 0.85
                )
            else:
                direction = 1.0 if dz >= 0.0 else -1.0
                prop.pos[2] += direction * (overlap_z + 0.5)
                push_speed = abs(pvz) / mass
                state['velocity_z'] = direction * max(
                    abs(state.get('velocity_z', 0.0)), push_speed * 0.85
                )

            p['_physics_awake'] = True

    def _prop_overlaps_brush(self, center, half, brush):
        if brush.get('_collision_mode') == 'mesh':
            bounds = brush.get('_mesh_bounds')
            if bounds:
                bmin, bmax = bounds
                return (
                    center[0] + half[0] > bmin[0] and
                    center[0] - half[0] < bmax[0] and
                    center[1] + half[1] > bmin[1] and
                    center[1] - half[1] < bmax[1] and
                    center[2] + half[2] > bmin[2] and
                    center[2] - half[2] < bmax[2]
                )
            return False

        bpos = brush.get('pos', (0.0, 0.0, 0.0))
        bsize = brush.get('size', (0.0, 0.0, 0.0))
        return (
            center[0] + half[0] > bpos[0] - bsize[0] * 0.5 and
            center[0] - half[0] < bpos[0] + bsize[0] * 0.5 and
            center[1] + half[1] > bpos[1] - bsize[1] * 0.5 and
            center[1] - half[1] < bpos[1] + bsize[1] * 0.5 and
            center[2] + half[2] > bpos[2] - bsize[2] * 0.5 and
            center[2] - half[2] < bpos[2] + bsize[2] * 0.5
        )

    def _move_horizontal(self, prop, state, axis, amount, center, half):
        if abs(amount) < 0.00001:
            return False

        before = list(prop.pos)
        if axis == 0:
            prop.pos[0] += amount
        else:
            prop.pos[2] += amount

        grid = getattr(self.logic, '_spatial_grid', None)
        query = getattr(grid, 'get_potential_colliders', None)
        if query is None:
            return True

        cx = center[0] + (prop.pos[0] - before[0])
        cz = center[2] + (prop.pos[2] - before[2])
        center_now = (cx, center[1], cz)
        min_pos = (
            type('_V', (), {'x': cx - half[0], 'y': center_now[1] - half[1], 'z': cz - half[2]})()
        )
        max_pos = (
            type('_V', (), {'x': cx + half[0], 'y': center_now[1] + half[1], 'z': cz + half[2]})()
        )
        try:
            colliders = query(min_pos, max_pos)
        except Exception:
            colliders = ()

        for brush in colliders:
            if brush.get('_dynamic_prop'):
                continue
            if self._prop_overlaps_brush(center_now, half, brush):
                prop.pos = before
                if axis == 0:
                    state['velocity_x'] = 0.0
                else:
                    state['velocity_z'] = 0.0
                return False
        return True

    def _pick_in_view(self, eye, forward):
        best = None
        best_distance = None
        for prop in self.props:
            p = prop.properties
            if p.get('disabled') or not p.get('pickup_enabled', True):
                continue
            dx, dy, dz = (float(prop.pos[0]) - eye[0], float(prop.pos[1]) - eye[1],
                          float(prop.pos[2]) - eye[2])
            distance = _length((dx, dy, dz))
            reach = float(p.get('pickup_reach', 110.0))
            if distance < 0.001 or distance > reach:
                continue
            if _dot(forward, (dx / distance, dy / distance, dz / distance)) < 0.86:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = prop, distance
        if best is not None:
            self.held = best
            self.moving.pop(id(best), None)
            self._fire(best, 'OnPickedUp')

    def _carry(self, eye, forward, use_pressed):
        prop = self.held
        p = prop.properties
        offset = p.get('carry_offset', [0.0, -6.0, 0.0])
        distance = float(p.get('carry_distance', 55.0))
        prop.pos = [eye[0] + forward[0] * distance + float(offset[0]),
                    eye[1] + forward[1] * distance + float(offset[1]),
                    eye[2] + forward[2] * distance + float(offset[2])]
        self.logic.current_hud_message = '[E] Drop'
        if use_pressed or p.pop('_drop_requested', False):
            self.held = None
            self.moving[id(prop)] = {'prop': prop, 'velocity': float(p.get('drop_velocity', 0.0))}
            p['_physics_awake'] = True
            self._fire(prop, 'OnDropped')

    def _floor_y(self, prop, x, z, from_y):
        # Physics controls motion; collision remains an independent Prop
        # setting so physics-without-collision is valid.
        if prop.properties.get('no_collision', True):
            return None
        grid = getattr(self.logic, '_spatial_grid', None)
        raycast = getattr(grid, 'raycast_down', None)
        if raycast is None:
            return None
        try:
            return raycast(x, z, from_y)
        except Exception:
            return None

    def _simulate(self, delta):
        dt = min(MAX_STEP, max(0.0, float(delta) or 1.0 / 60.0))
        sleeping = []
        for key, state in self.moving.items():
            prop = state['prop']
            p = prop.properties
            if prop is self.held or p.get('disabled'):
                sleeping.append(key)
                continue
            if not p.get('physics_enabled', False):
                sleeping.append(key)
                continue
            velocity = state.get('velocity', 0.0)
            if p.get('gravity', True):
                velocity += GRAVITY * dt
            velocity *= max(0.0, 1.0 - float(p.get('linear_damping', 0.08)) * dt)
            state['velocity'] = velocity

            vx = float(state.get('velocity_x', 0.0))
            vz = float(state.get('velocity_z', 0.0))
            friction = max(0.0, min(1.0, float(p.get('friction', 0.55))))
            horizontal_damp = max(
                0.0,
                1.0 - (float(p.get('linear_damping', 0.08)) + friction) * dt,
            )
            vx *= horizontal_damp
            vz *= horizontal_damp
            state['velocity_x'] = vx
            state['velocity_z'] = vz

            shape = self._shape_for(prop)
            x, y, z = _vec(prop.pos)
            if shape is not None:
                off = shape['offset']
                half = tuple(float(v) * 0.5 for v in shape['size'])
                center = (x + off[0], y + off[1], z + off[2])
                self._move_horizontal(prop, state, 0, vx * dt, center, half)
                center = (prop.pos[0] + off[0], y + off[1], prop.pos[2] + off[2])
                self._move_horizontal(prop, state, 1, vz * dt, center, half)

            x, y, z = _vec(prop.pos)
            new_y = y + velocity * dt
            floor = self._floor_y(prop, x, z, y + 1.0)
            if floor is not None and new_y <= floor:
                prop.pos = [x, floor, z]
                velocity = 0.0
                state['velocity'] = 0.0
                if (abs(state.get('velocity_x', 0.0)) < 1.0 and
                        abs(state.get('velocity_z', 0.0)) < 1.0):
                    sleeping.append(key)
                    p['_physics_awake'] = False
                    self._fire(prop, 'OnRest')
                    continue
            else:
                prop.pos = [x, new_y, z]

            if (abs(state.get('velocity_x', 0.0)) < 1.0 and
                    abs(state.get('velocity_z', 0.0)) < 1.0 and
                    abs(velocity) < 1.0):
                state['velocity_x'] = 0.0
                state['velocity_z'] = 0.0
                sleeping.append(key)
                p['_physics_awake'] = False
                self._fire(prop, 'OnRest')
                continue

            rotation = p.get('rotation', [0.0, 0.0, 0.0])
            angular = p.get('drop_angular_velocity', [0.0, 0.0, 0.0])
            p['rotation'] = [float(rotation[i]) + float(angular[i]) * dt for i in range(3)]
        for key in sleeping:
            self.moving.pop(key, None)
