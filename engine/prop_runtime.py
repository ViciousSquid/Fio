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
        if self.moving:
            self._simulate(delta)

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
            velocity = state['velocity']
            if p.get('gravity', True):
                velocity += GRAVITY * dt
            velocity *= max(0.0, 1.0 - float(p.get('linear_damping', 0.08)) * dt)
            state['velocity'] = velocity
            x, y, z = _vec(prop.pos)
            new_y = y + velocity * dt
            floor = self._floor_y(prop, x, z, y + 1.0)
            if floor is not None and new_y <= floor:
                prop.pos = [x, floor, z]
                sleeping.append(key)
                p['_physics_awake'] = False
                self._fire(prop, 'OnRest')
                continue
            prop.pos = [x, new_y, z]
            rotation = p.get('rotation', [0.0, 0.0, 0.0])
            angular = p.get('drop_angular_velocity', [0.0, 0.0, 0.0])
            p['rotation'] = [float(rotation[i]) + float(angular[i]) * dt for i in range(3)]
        for key in sleeping:
            self.moving.pop(key, None)
