"""Persistent dense storage for live monster projectiles.

The gameplay-facing projectile records remain ordinary dicts for compatibility with
existing I/O/debug/test code. Numeric simulation state lives in reusable NumPy
arrays so the 60 Hz update does not rebuild position/velocity/lifetime/distance
arrays from Python dictionaries every tick.
"""

from __future__ import annotations

import numpy as np


class ProjectileStore(list):
    """List-compatible projectile records backed by reusable numeric columns."""

    __slots__ = (
        "pos",
        "vel",
        "lifetime",
        "distance",
        "owner_id",
        "_capacity",
        "_records_dirty",
    )

    def __init__(self, records=(), capacity=0):
        super().__init__(records)
        initial = len(self)
        self._capacity = max(16, int(capacity), initial)
        self.pos = np.empty((self._capacity, 3), dtype=np.float64)
        self.vel = np.empty((self._capacity, 3), dtype=np.float64)
        self.lifetime = np.empty(self._capacity, dtype=np.float64)
        self.distance = np.empty(self._capacity, dtype=np.float64)
        self.owner_id = np.empty(self._capacity, dtype=np.int64)
        self._records_dirty = False
        if initial:
            self.sync_from_records()

    def _ensure_capacity(self, required):
        if required <= self._capacity:
            return
        new_capacity = max(16, self._capacity * 2, required)
        self.pos = np.resize(self.pos, (new_capacity, 3))
        self.vel = np.resize(self.vel, (new_capacity, 3))
        self.lifetime = np.resize(self.lifetime, new_capacity)
        self.distance = np.resize(self.distance, new_capacity)
        self.owner_id = np.resize(self.owner_id, new_capacity)
        self._capacity = new_capacity

    @staticmethod
    def _vec3(record, key):
        value = record.get(key, (0.0, 0.0, 0.0))
        return float(value[0]), float(value[1]), float(value[2])

    def append(self, record):
        index = len(self)
        self._ensure_capacity(index + 1)
        super().append(record)
        self._write_record(index, record)

    def extend(self, records):
        records = list(records)
        if not records:
            return
        start = len(self)
        self._ensure_capacity(start + len(records))
        super().extend(records)
        for offset, record in enumerate(records):
            self._write_record(start + offset, record)

    def insert(self, index, record):
        super().insert(index, record)
        self._records_dirty = True

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._records_dirty = True

    def __delitem__(self, key):
        super().__delitem__(key)
        self._records_dirty = True

    def clear(self):
        super().clear()
        self._records_dirty = False

    def pop(self, *args):
        value = super().pop(*args)
        self._records_dirty = True
        return value

    def _write_record(self, index, record):
        self.pos[index] = self._vec3(record, "pos")
        self.vel[index] = self._vec3(record, "vel")
        self.lifetime[index] = float(record.get("lifetime", 0.0))
        self.distance[index] = float(record.get("distance_travelled", 0.0))
        self.owner_id[index] = int(record.get("owner_id", 0))

    def sync_from_records(self):
        """Rebuild numeric columns once after external record-level edits."""
        self._ensure_capacity(len(self))
        for index, record in enumerate(self):
            self._write_record(index, record)
        self._records_dirty = False

    def sync_if_dirty(self):
        if self._records_dirty:
            self.sync_from_records()

    def replace_active(self, survivors, pos, vel, lifetime, distance):
        """Compact live records and numeric columns without reallocating normally."""
        indices = np.asarray(survivors, dtype=np.intp)
        count = int(indices.size)

        if count:
            kept = [self[int(i)] for i in indices.tolist()]
            new_pos = np.take(pos, indices, axis=0)
            new_vel = np.take(vel, indices, axis=0)
            new_lifetime = np.take(lifetime, indices)
            new_distance = np.take(distance, indices)
        else:
            kept = []
            new_pos = np.empty((0, 3), dtype=np.float64)
            new_vel = np.empty((0, 3), dtype=np.float64)
            new_lifetime = np.empty(0, dtype=np.float64)
            new_distance = np.empty(0, dtype=np.float64)

        # Update the compatibility records only after all dense gathers are
        # complete. The simulation itself has stayed entirely in the columns.
        for index, record in enumerate(kept):
            p = record.get("pos")
            v = record.get("vel")
            if isinstance(p, list) and len(p) == 3:
                p[0], p[1], p[2] = map(float, new_pos[index])
            else:
                record["pos"] = new_pos[index].tolist()
            if isinstance(v, list) and len(v) == 3:
                v[0], v[1], v[2] = map(float, new_vel[index])
            else:
                record["vel"] = new_vel[index].tolist()
            record["distance_travelled"] = float(new_distance[index])
            record["lifetime"] = float(new_lifetime[index])

        super().__setitem__(slice(None), kept)
        if count:
            self.pos[:count] = new_pos
            self.vel[:count] = new_vel
            self.lifetime[:count] = new_lifetime
            self.distance[:count] = new_distance
            self.owner_id[:count] = [
                int(record.get("owner_id", 0)) for record in kept
            ]
        self._records_dirty = False

    @property
    def count(self):
        return len(self)
