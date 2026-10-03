"""Persistent structure-of-arrays storage for live monster projectiles.

Projectile simulation state is numeric and contiguous. There are no per-projectile
Python dictionaries: the logic thread owns position, velocity, lifetime, distance,
owner and damage as NumPy columns.
"""

from __future__ import annotations

import numpy as np

from .soa import grow_soa_arrays


class ProjectileStore:
    """Dense SoA store for monster projectiles."""

    __slots__ = (
        "pos", "vel", "lifetime", "distance", "owner_id", "damage",
        "_capacity", "_count",
    )

    def __init__(self, capacity=16):
        self._capacity = max(1, int(capacity))
        self._count = 0
        self.pos = np.empty((self._capacity, 3), dtype=np.float32)
        self.vel = np.empty((self._capacity, 3), dtype=np.float32)
        self.lifetime = np.empty(self._capacity, dtype=np.float32)
        self.distance = np.empty(self._capacity, dtype=np.float32)
        self.owner_id = np.empty(self._capacity, dtype=np.int64)
        self.damage = np.empty(self._capacity, dtype=np.float32)

    def __len__(self):
        return self._count

    @property
    def count(self):
        return self._count

    def _ensure_capacity(self, required):
        if required <= self._capacity:
            return
        self._capacity, (
            self.pos,
            self.vel,
            self.lifetime,
            self.distance,
            self.owner_id,
            self.damage,
        ) = grow_soa_arrays(
            required,
            self._capacity,
            self.pos,
            self.vel,
            self.lifetime,
            self.distance,
            self.owner_id,
            self.damage,
        )

    def add(self, pos, vel, owner_id, damage, lifetime):
        i = self._count
        self._ensure_capacity(i + 1)
        self.pos[i] = pos
        self.vel[i] = vel
        self.owner_id[i] = int(owner_id)
        self.damage[i] = float(damage)
        self.lifetime[i] = float(lifetime)
        self.distance[i] = 0.0
        self._count = i + 1
        return i

    def add_batch(self, positions, velocities, owners, damages, lifetimes):
        positions = np.asarray(positions, dtype=np.float32)
        velocities = np.asarray(velocities, dtype=np.float32)
        owners = np.asarray(owners, dtype=np.int64)
        damages = np.asarray(damages, dtype=np.float32)
        lifetimes = np.asarray(lifetimes, dtype=np.float32)
        n = len(positions)
        if positions.shape != (n, 3) or velocities.shape != (n, 3):
            raise ValueError("projectile positions and velocities must be (N, 3)")
        if any(len(values) != n for values in (owners, damages, lifetimes)):
            raise ValueError("projectile batch columns must have equal length")
        if not n:
            return
        start = self._count
        stop = start + n
        self._ensure_capacity(stop)
        self.pos[start:stop] = positions
        self.vel[start:stop] = velocities
        self.owner_id[start:stop] = owners
        self.damage[start:stop] = damages
        self.lifetime[start:stop] = lifetimes
        self.distance[start:stop] = 0.0
        self._count = stop

    def clear(self):
        self._count = 0

    def compact(self, keep, pos, vel, lifetime, distance):
        keep = np.asarray(keep, dtype=np.intp)
        n = int(keep.size)
        if n:
            self.pos[:n] = np.take(pos, keep, axis=0)
            self.vel[:n] = np.take(vel, keep, axis=0)
            self.lifetime[:n] = np.take(lifetime, keep)
            self.distance[:n] = np.take(distance, keep)
            self.owner_id[:n] = np.take(self.owner_id[:self._count], keep)
            self.damage[:n] = np.take(self.damage[:self._count], keep)
        self._count = n
