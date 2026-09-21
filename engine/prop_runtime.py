"""Core interaction runtime for generic carryable Prop entities.

Physics simulation itself lives in :mod:`engine.physics`; this module only
handles pickup, carrying, dropping and Prop-specific I/O.
"""
from __future__ import annotations

import math


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
    """Pickup/carry/drop orchestration for core Props."""

    def __init__(self, logic):
        self.logic = logic
        self.props = []
        self.held = None

    @staticmethod
    def has_props(things):
        return any(getattr(t, "properties", {}).get("type") == "prop" for t in things)

    @property
    def physics(self):
        return getattr(self.logic, "_physics_world", None)

    def start(self):
        self.props = [t for t in self.logic.things
                      if getattr(t, "properties", {}).get("type") == "prop"]
        self.held = None
        for prop in self.props:
            prop.properties["_prop_home_pos"] = list(prop.pos)
            prop.properties.pop("_drop_requested", None)
            if self.physics is not None:
                self.physics.set_rest_callback(prop, self._on_rest)
                self.physics.set_kinematic(prop, False)

    def stop(self):
        for prop in self.props:
            home = prop.properties.pop("_prop_home_pos", None)
            prop.properties.pop("_drop_requested", None)
            if self.physics is not None:
                self.physics.set_rest_callback(prop, None)
                self.physics.set_kinematic(prop, False)
            if home is not None:
                prop.pos = home
        self.held = None
        self.props.clear()

    def is_empty(self):
        live = {id(t) for t in self.logic.things
                if getattr(t, "properties", {}).get("type") == "prop"}
        if len(live) == len(self.props) and all(id(prop) in live for prop in self.props):
            return False
        self.props = [prop for prop in self.props if id(prop) in live]
        if self.held is not None and id(self.held) not in live:
            self.held = None
        return not self.props

    def _fire(self, prop, output):
        io = getattr(self.logic, "io_manager", None)
        if io is not None:
            io.fire_output(prop, output)

    def _on_rest(self, prop):
        self._fire(prop, "OnRest")

    def tick(self, delta, use_pressed):
        del delta
        player = getattr(self.logic, "player", None)
        if player is None:
            return
        eye_pos = _vec(player.pos)
        eye = (eye_pos[0], eye_pos[1] + float(getattr(player, "camera_height", 40.0)), eye_pos[2])
        forward = _forward(player)
        if self.held is not None:
            self._carry(eye, forward, use_pressed)
        elif use_pressed:
            self._pick_in_view(eye, forward)

    def _pick_in_view(self, eye, forward):
        best = None
        best_distance = None
        for prop in self.props:
            p = prop.properties
            if p.get("disabled") or not p.get("pickup_enabled", True):
                continue
            dx = float(prop.pos[0]) - eye[0]
            dy = float(prop.pos[1]) - eye[1]
            dz = float(prop.pos[2]) - eye[2]
            distance = _length((dx, dy, dz))
            reach = float(p.get("pickup_reach", 110.0))
            if distance < 0.001 or distance > reach:
                continue
            if _dot(forward, (dx / distance, dy / distance, dz / distance)) < 0.86:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = prop, distance
        if best is not None:
            self.held = best
            if self.physics is not None:
                self.physics.set_kinematic(best, True)
            self._fire(best, "OnPickedUp")

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
        self.logic.current_hud_message = "[E] Drop"
        if use_pressed or p.pop("_drop_requested", False):
            # A gameplay plugin may consume the drop (for example, a Tidy
            # receptacle placement) while the core Prop still owns pickup,
            # carrying and the eventual ordinary drop.
            interceptor = getattr(self.logic, "_prop_drop_interceptor", None)
            if interceptor is not None:
                try:
                    if interceptor(prop):
                        return
                except Exception:
                    # A broken plugin must not prevent the core prop from
                    # dropping normally.
                    pass

            self.held = None
            if self.physics is not None:
                self.physics.set_kinematic(prop, False)
                self.physics.wake(prop, [0.0, float(p.get("drop_velocity", 0.0)), 0.0])
            self._fire(prop, "OnDropped")
