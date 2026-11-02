"""Pure, fixed-step, ground-plane arcade driving; no Qt, OpenGL or NumPy."""
from __future__ import annotations

from dataclasses import dataclass
import math

MAX_FORWARD = 410.0
MAX_REVERSE = 145.0
ACCELERATION = 290.0
BRAKING = 470.0
COAST_DRAG = 1.55
STEER_RATE = 2.35
COLLISION_RADIUS = 29.0


def clamp(value, low, high):
    return max(low, min(high, value))


def angle_delta(target: float, current: float) -> float:
    """Shortest signed change from current to target (radians)."""
    return (target - current + math.pi) % (2.0 * math.pi) - math.pi


def circle_hits_rect(x, z, r, rect):
    """Circle versus closed axis-aligned X/Z rectangle."""
    left, top, right, bottom = rect
    nearest_x = clamp(x, left, right)
    nearest_z = clamp(z, top, bottom)
    return (x - nearest_x) ** 2 + (z - nearest_z) ** 2 < r * r


@dataclass
class Vehicle:
    x: float
    z: float
    angle: float = 0.0                 # Fio: forward is (sin a, cos a)
    speed: float = 0.0
    impacts: int = 0

    def step(self, dt, throttle, steering, handbrake=False, collides=None):
        """Integrate one tick, with substeps to prevent tunnelling through walls.

        Returns True exactly once for an impacting tick. World units are 10 cm.
        """
        dt = clamp(float(dt), 0.0, 0.10)
        if dt == 0:
            return False
        throttle = clamp(float(throttle), -1.0, 1.0)
        steering = clamp(float(steering), -1.0, 1.0)
        if throttle > 0:
            self.speed += (ACCELERATION if self.speed >= 0 else BRAKING) * dt * throttle
        elif throttle < 0:
            self.speed += (BRAKING if self.speed > 0 else ACCELERATION * 0.7) * dt * throttle
        else:
            self.speed *= math.exp(-(COAST_DRAG * (3.0 if handbrake else 1.0)) * dt)
            if abs(self.speed) < 0.5:
                self.speed = 0.0
        if handbrake:
            self.speed *= math.exp(-1.7 * dt)
        self.speed = clamp(self.speed, -MAX_REVERSE, MAX_FORWARD)

        # Steer only when rolling; reversing reverses the turning direction.
        turn_factor = clamp(abs(self.speed) / 125.0, 0.0, 1.0)
        turn = STEER_RATE * (1.55 if handbrake else 1.0) * steering * turn_factor
        turn *= 1.0 if self.speed >= 0 else -1.0
        # Keep an entire step shorter than the collision circle's diameter.
        n = max(1, math.ceil(abs(self.speed * dt) / (COLLISION_RADIUS * 0.65)))
        segment = dt / n
        for _ in range(n):
            self.angle = (self.angle + turn * segment + math.pi) % (2 * math.pi) - math.pi
            nx = self.x + math.sin(self.angle) * self.speed * segment
            nz = self.z + math.cos(self.angle) * self.speed * segment
            if collides is not None and collides(nx, nz, COLLISION_RADIUS):
                self.speed *= -0.25
                self.impacts += 1
                return True
            self.x, self.z = nx, nz
        return False


@dataclass
class Traffic:
    vehicle: Vehicle
    route: tuple
    cruise_speed: float
    next_waypoint: int = 1

    def update(self, dt):
        """Advance a lightweight loop of authored road waypoints."""
        if len(self.route) < 2:
            return
        dt = clamp(float(dt), 0.0, 0.10)
        target = self.route[self.next_waypoint]
        dx = target[0] - self.vehicle.x
        dz = target[1] - self.vehicle.z
        distance = math.hypot(dx, dz)
        if distance < 52.0:
            self.next_waypoint = (self.next_waypoint + 1) % len(self.route)
            target = self.route[self.next_waypoint]
            dx = target[0] - self.vehicle.x
            dz = target[1] - self.vehicle.z
            distance = math.hypot(dx, dz)
        if distance < 1e-5:
            return
        wanted = math.atan2(dx, dz)
        diff = angle_delta(wanted, self.vehicle.angle)
        turn = clamp(diff, -2.6 * dt, 2.6 * dt)
        self.vehicle.angle += turn
        # Slow into bends instead of cutting through corners.
        speed = self.cruise_speed * clamp(1.0 - abs(diff) / 2.5, 0.35, 1.0)
        step = min(distance, speed * dt)
        self.vehicle.x += math.sin(self.vehicle.angle) * step
        self.vehicle.z += math.cos(self.vehicle.angle) * step
