"""Dense structure-of-arrays execution state for Effect primitives.

Effect objects remain the authoring representation.  This store owns the
simulation/runtime state so the logic and render projections never need to
read or mutate per-Effect runtime attributes on the hot path.
"""

from __future__ import annotations

import random

import numpy as np

from .effect_entity import (
    EFFECT_CUSTOM,
    EFFECT_EXPLOSION,
    EFFECT_FIRE,
    EFFECT_ORB,
)


FAMILY_FIRE = 0
FAMILY_EXPLOSION = 1
FAMILY_ORB = 2
FAMILY_CUSTOM = 3

_FAMILY_IDS = {
    EFFECT_FIRE: FAMILY_FIRE,
    EFFECT_EXPLOSION: FAMILY_EXPLOSION,
    EFFECT_ORB: FAMILY_ORB,
    EFFECT_CUSTOM: FAMILY_CUSTOM,
}


def effect_family(value) -> int:
    """Resolve an authored Effect type to its dense execution family id."""
    return _FAMILY_IDS.get(
        str(value or EFFECT_FIRE).strip().upper(),
        FAMILY_FIRE,
    )


class EffectStore:
    """Dense SoA runtime state for authored Effect entities.

    The store deliberately keeps no references to the authoring objects.
    The id(object) -> row mapping is only a transient lookup key; the arrays
    are the authoritative execution state.
    """

    __slots__ = (
        "pos",
        "lifetime",
        "phase",
        "family_id",
        "spawn_time",
        "active",
        "ids",
        "_index_by_object",
        "_capacity",
        "_count",
    )

    def __init__(self, capacity=16):
        self._capacity = max(1, int(capacity))
        self._count = 0
        self.pos = np.empty((self._capacity, 3), dtype=np.float32)
        self.lifetime = np.empty(self._capacity, dtype=np.float32)
        self.phase = np.empty(self._capacity, dtype=np.float32)
        self.family_id = np.empty(self._capacity, dtype=np.uint8)
        self.spawn_time = np.empty(self._capacity, dtype=np.float64)
        self.active = np.empty(self._capacity, dtype=bool)
        self.ids = []
        self._index_by_object = {}

    def __len__(self):
        return self._count

    @property
    def count(self):
        return self._count

    def _ensure_capacity(self, required):
        if required <= self._capacity:
            return
        new_capacity = max(required, self._capacity * 2)
        self.pos = np.resize(self.pos, (new_capacity, 3))
        self.lifetime = np.resize(self.lifetime, new_capacity)
        self.phase = np.resize(self.phase, new_capacity)
        self.family_id = np.resize(self.family_id, new_capacity)
        self.spawn_time = np.resize(self.spawn_time, new_capacity)
        self.active = np.resize(self.active, new_capacity)
        self._capacity = new_capacity

    @staticmethod
    def _lifetime(thing) -> float:
        try:
            return max(0.01, float(thing.properties.get("lifetime", 0.5)))
        except (TypeError, ValueError):
            return 0.5

    @staticmethod
    def _position(thing):
        pos = thing.pos
        return (float(pos[0]), float(pos[1]), float(pos[2]))

    def clear(self):
        """Drop all execution rows without retaining authoring objects."""
        self._count = 0
        self.ids.clear()
        self._index_by_object.clear()

    def rebuild(self, things, *, reset_runtime=False):
        """Reconcile execution rows to the current authoring Effect set.

        Existing object identities retain runtime state unless reset_runtime is
        requested. Added rows receive fresh runtime state; removed rows
        disappear from the dense store.
        """
        old_count = self._count
        old_index = self._index_by_object
        old_phase = self.phase
        old_spawn = self.spawn_time
        old_active = self.active

        self._count = 0
        self.ids = []
        self._index_by_object = {}

        for thing in things:
            props = getattr(thing, "properties", None)
            if not isinstance(props, dict) or props.get("type") != "effect":
                continue

            old = old_index.get(id(thing))
            i = self._count
            self._ensure_capacity(i + 1)
            self._index_by_object[id(thing)] = i
            self.ids.append(props.get("id"))
            self.pos[i] = self._position(thing)
            self.lifetime[i] = self._lifetime(thing)
            self.family_id[i] = effect_family(
                props.get("effect_type", EFFECT_FIRE)
            )

            if not reset_runtime and old is not None and old < old_count:
                self.phase[i] = old_phase[old]
                self.spawn_time[i] = old_spawn[old]
                self.active[i] = old_active[old]
            else:
                self.phase[i] = random.random()
                self.spawn_time[i] = 0.0
                self.active[i] = self.family_id[i] != FAMILY_EXPLOSION
            self._count = i + 1

    def begin_session(self, things):
        """Reset every Effect runtime row at Play start."""
        self.rebuild(things, reset_runtime=True)

    def index_of(self, thing):
        """Return the dense execution row for an Effect, or -1."""
        return int(self._index_by_object.get(id(thing), -1))

    def sync_authored(self, thing):
        """Refresh cold authoring-derived state for one Effect row."""
        index = self.index_of(thing)
        if index < 0:
            self.rebuild([thing])
            return self.index_of(thing)

        props = getattr(thing, "properties", {})
        self.pos[index] = self._position(thing)
        self.lifetime[index] = self._lifetime(thing)
        self.family_id[index] = effect_family(
            props.get("effect_type", EFFECT_FIRE)
        )
        return index

    def set_type(self, thing, value):
        """Apply a SetType runtime reset to one dense execution row."""
        index = self.sync_authored(thing)
        self.family_id[index] = effect_family(value)
        self.phase[index] = random.random()
        self.spawn_time[index] = 0.0
        self.active[index] = self.family_id[index] != FAMILY_EXPLOSION
        return index

    def trigger_explosion(self, thing, now):
        """Start or restart one-shot EXPLOSION playback."""
        index = self.sync_authored(thing)
        self.family_id[index] = FAMILY_EXPLOSION
        self.phase[index] = 0.0
        self.spawn_time[index] = float(now)
        self.active[index] = True
        return index

    def set_position(self, thing, position):
        """Update an Effect execution position without reading object state."""
        index = self.index_of(thing)
        if index >= 0:
            self.pos[index] = position
