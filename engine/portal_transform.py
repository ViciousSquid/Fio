"""Numerical portal frame and link transforms.

Portal authoring objects live in editor.things; runtime consumers should
not depend on those objects for the spatial algebra. This module is GL-free
and contains the single spatial transform used by rendering and gameplay.
"""

from __future__ import annotations

import math


def basis_from_rotation(rotation):
    """Return (right, up, normal) from [yaw, pitch, roll] degrees."""
    try:
        yaw = math.radians(float(rotation[0]))
        pitch = math.radians(float(rotation[1]))
        roll = math.radians(float(rotation[2]))
    except (TypeError, ValueError, IndexError):
        yaw = pitch = roll = 0.0

    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    right = (cy * cr + sy * sp * sr, cp * sr, -sy * cr + cy * sp * sr)
    up = (-cy * sr + sy * sp * cr, cp * cr, sy * sr + cy * sp * cr)
    normal = (sy * cp, -sp, cy * cp)
    return right, up, normal


def map_point(src_pos, src_basis, dst_pos, dst_basis, point):
    """Map a world point through a portal pair."""
    dx = float(point[0]) - float(src_pos[0])
    dy = float(point[1]) - float(src_pos[1])
    dz = float(point[2]) - float(src_pos[2])
    r, u, n = src_basis
    lr = dx * r[0] + dy * r[1] + dz * r[2]
    lu = dx * u[0] + dy * u[1] + dz * u[2]
    ln = dx * n[0] + dy * n[1] + dz * n[2]
    lr, ln = -lr, -ln
    r2, u2, n2 = dst_basis
    return (float(dst_pos[0]) + lr * r2[0] + lu * u2[0] + ln * n2[0],
            float(dst_pos[1]) + lr * r2[1] + lu * u2[1] + ln * n2[1],
            float(dst_pos[2]) + lr * r2[2] + lu * u2[2] + ln * n2[2])


def map_direction(src_basis, dst_basis, direction):
    """Map a world direction or velocity through a portal pair."""
    r, u, n = src_basis
    x, y, z = float(direction[0]), float(direction[1]), float(direction[2])
    lr = x * r[0] + y * r[1] + z * r[2]
    lu = x * u[0] + y * u[1] + z * u[2]
    ln = x * n[0] + y * n[1] + z * n[2]
    lr, ln = -lr, -ln
    r2, u2, n2 = dst_basis
    return (lr * r2[0] + lu * u2[0] + ln * n2[0],
            lr * r2[1] + lu * u2[1] + ln * n2[1],
            lr * r2[2] + lu * u2[2] + ln * n2[2])


def corners(pos, basis, width, height):
    """Return the four world-space aperture corners."""
    px, py, pz = map(float, pos)
    w2 = max(16.0, float(width)) * 0.5
    h2 = max(16.0, float(height)) * 0.5
    r, u, _ = basis
    return [
        [px - r[0] * w2 - u[0] * h2, py - r[1] * w2 - u[1] * h2, pz - r[2] * w2 - u[2] * h2],
        [px + r[0] * w2 - u[0] * h2, py + r[1] * w2 - u[1] * h2, pz + r[2] * w2 - u[2] * h2],
        [px + r[0] * w2 + u[0] * h2, py + r[1] * w2 + u[1] * h2, pz + r[2] * w2 + u[2] * h2],
        [px - r[0] * w2 + u[0] * h2, py - r[1] * w2 + u[1] * h2, pz - r[2] * w2 + u[2] * h2],
    ]


def contains_point(pos, basis, width, height, point, margin=0.0):
    """Return whether a point lies inside the portal aperture rectangle."""
    dx = float(point[0]) - float(pos[0])
    dy = float(point[1]) - float(pos[1])
    dz = float(point[2]) - float(pos[2])
    r, u, n = basis
    lr = dx * r[0] + dy * r[1] + dz * r[2]
    lu = dx * u[0] + dy * u[1] + dz * u[2]
    ln = dx * n[0] + dy * n[1] + dz * n[2]
    hw = max(16.0, float(width)) * 0.5 + float(margin)
    hh = max(16.0, float(height)) * 0.5 + float(margin)
    # The aperture is a plane, not an infinite rectangle extruded along its
    # normal. Keep only a tiny numerical tolerance on that third coordinate.
    plane_eps = max(1e-6, abs(float(margin)))
    return (abs(lr) <= hw and abs(lu) <= hh and abs(ln) <= plane_eps)
