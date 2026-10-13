"""Rolling bodies: how a ball turns as it moves, and how it bounces.

A Prop with ``rolls`` set is drawn as a sphere rolling inside its collision
box. The box still does all the colliding -- walls, floors, the player's push
-- exactly as for any other Prop. Rolling only adds, cheaply:

* **Spin from speed.** A ball rolling without slipping turns at ``v / r`` about
  the horizontal axis square to its motion, so its spin is read straight from
  its velocity: ``w = (vz, 0, -vx) / r``. There is no spin state to simulate.
* **Orientation** is a unit quaternion turned by that spin each step, so rolls
  in different directions compose the way a real ball's do (adding Euler
  angles does not). It becomes the Prop's ``rotation`` only to draw it.
* **Rolling resistance.** A rolling ball loses far less speed to the ground
  than a sliding box, so on the floor it gets :data:`ROLLING_FRICTION_SCALE`
  of its ``friction``.
* **Bounce** reflects the speed a body hits the floor or a wall with, scaled by
  its ``bounciness`` and faded out by its ``mass``.
* **Heavy balls** (``mass`` at or above :data:`HEAVY_MASS`) do not bounce, and
  the player cannot push one: it pushes the player out of its way instead, and
  crushes a player it pins against a wall.

Everything is NumPy over the rows it is given.
"""

from __future__ import annotations

import numpy as np

#: The share of a body's ``friction`` that slows it while it rolls.
ROLLING_FRICTION_SCALE = 0.05
#: Mass at and above which a ball is heavy: it does not bounce, and it pushes
#: the player rather than being pushed. A body of mass 1 or less bounces by its
#: full ``bounciness``; between the two the bounce fades linearly, so a heavier
#: ball bounces lower.
HEAVY_MASS = 10.0
#: A heavy ball rolling at the player faster than this (units per second)
#: crushes them if it pins them against a wall.
CRUSH_SPEED = 5.0
#: Damage a crushing ball does: enough to kill through any armour. God mode
#: still stops it, as it stops all player damage.
CRUSH_DAMAGE = 100000
#: A rebound slower than this (units per second) is not a bounce: the body
#: settles instead of chattering on the floor.
MIN_BOUNCE_SPEED = 60.0


def restitution(bounciness, mass):
    """The fraction of impact speed a body keeps when it bounces.

    *bounciness* (0..1) is what the author asked for; *mass* takes it away, to
    nothing at :data:`HEAVY_MASS`. Scalars or arrays.
    """
    bounciness = np.clip(np.asarray(bounciness, dtype=np.float32), 0.0, 1.0)
    mass = np.asarray(mass, dtype=np.float32)
    weight = np.clip((HEAVY_MASS - mass) / (HEAVY_MASS - 1.0),
                     0.0, 1.0)
    return (bounciness * weight).astype(np.float32)


def bounce_speed(impact_speed, coefficient):
    """Rebound speed for an impact, or 0 where it is too slow to bounce."""
    rebound = np.asarray(impact_speed, dtype=np.float32) * coefficient
    return np.where(rebound > MIN_BOUNCE_SPEED, rebound, 0.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Orientation: quaternions are (w, x, y, z) rows, in float64 so a ball rolling
# for minutes keeps a unit quaternion.
# ---------------------------------------------------------------------------

def _quat_multiply(a, b):
    aw, ax, ay, az = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    bw, bx, by, bz = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    return np.stack((
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    ), axis=1)


def _axis_quat(axis, radians):
    q = np.zeros((radians.shape[0], 4), dtype=np.float64)
    q[:, 0] = np.cos(radians * 0.5)
    q[:, 1 + axis] = np.sin(radians * 0.5)
    return q


def quat_from_rotation(rotation):
    """Quaternions for Fio ``rotation`` rows ``[x, y, z]`` in degrees.

    Fio draws a model with ``Ry(y) * Rx(x) * Rz(z)`` (see
    ``engine.entity_table._model_transform_columns``); this is that product.
    """
    r = np.radians(np.asarray(rotation, dtype=np.float64).reshape(-1, 3))
    return _quat_multiply(_quat_multiply(_axis_quat(1, r[:, 1]),
                                         _axis_quat(0, r[:, 0])),
                          _axis_quat(2, r[:, 2]))


def rotation_from_quat(q):
    """Fio ``rotation`` rows (Y-X-Z Euler degrees) for (N, 4) quaternions."""
    q = np.asarray(q, dtype=np.float64).reshape(-1, 4)
    w, x, y, z = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    r12 = 2.0 * (y * z - w * x)
    pitch = np.arcsin(np.clip(-r12, -1.0, 1.0))
    yaw = np.arctan2(2.0 * (x * z + w * y), 1.0 - 2.0 * (x * x + y * y))
    roll = np.arctan2(2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z))
    # Looking straight up or down, the Y and Z turns are one turn: all yaw.
    locked = np.abs(r12) > 0.999999
    if np.any(locked):
        yaw[locked] = np.arctan2(-2.0 * (x * z - w * y)[locked],
                                 (1.0 - 2.0 * (y * y + z * z))[locked])
        roll[locked] = 0.0
    return np.degrees(np.stack((pitch, yaw, roll), axis=1))


def roll(q, velocity, radius, dt):
    """Turn quaternions *q* as balls of *radius* roll at *velocity* for *dt*.

    Rolling without slipping, the ball turns ``|v_horizontal| * dt / r``
    radians about ``(vz, 0, -vx)`` -- the horizontal axis square to its motion.
    """
    vx = np.asarray(velocity[:, 0], dtype=np.float64)
    vz = np.asarray(velocity[:, 2], dtype=np.float64)
    speed = np.hypot(vx, vz)
    moving = speed > 1e-9
    safe = np.where(moving, speed, 1.0)
    half = 0.5 * speed * float(dt) / np.maximum(np.asarray(radius, dtype=np.float64), 1e-6)
    s = np.sin(half) / safe
    turn = np.zeros((q.shape[0], 4), dtype=np.float64)
    turn[:, 0] = np.cos(half)
    turn[:, 1] = vz * s
    turn[:, 3] = -vx * s
    turn[~moving] = (1.0, 0.0, 0.0, 0.0)
    # The axis is fixed in the world, so the turn is applied on the left.
    out = _quat_multiply(turn, q)
    out /= np.linalg.norm(out, axis=1, keepdims=True)
    return out
