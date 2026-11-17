"""Fio's physics boundary.

The engine (the logic thread and everything on it) depends only on what this
package exports:

* :class:`Physics` and :class:`Body` -- the contract every physics implements
  (``api``), with what crosses it: :data:`BODY_BRUSH`, :data:`BODY_PROPERTIES`
  and :data:`WORLD_QUERIES`
* the game's rules a physics honours: :data:`SV_GRAVITY`, :data:`HEAVY_MASS`,
  :data:`CRUSH_DAMAGE`, and :func:`world_gravity`
* the registry: :func:`register_physics`, :func:`create_physics`,
  :func:`select_physics`, :func:`available_physics`, :data:`DEFAULT_PHYSICS`

Implementations are reached through the registry: ``soma`` is Fio's
built-in physics: Soma. The solid world is not physics' to hold: it is
:class:`engine.spatial_grid.SpatialGrid`, engine core, which a physics is
created with and asks. Importing this package needs neither NumPy nor PyGLM.

``SpatialGrid``, ``PhysicsWorld`` (now ``Soma``) and ``PhysicsBody`` used to
live in a module of this name; they are still importable from here (resolved on first use) so
existing imports keep working. New code names their real homes.
"""

import importlib

from .api import (
    BODY_BRUSH, BODY_PROPERTIES, CRUSH_DAMAGE, HEAVY_MASS, SV_GRAVITY,
    WORLD_QUERIES, Body, Physics, world_gravity)
from .registry import (
    DEFAULT_PHYSICS, available_physics, create_physics, physics_factory,
    register_physics, select_physics, selected_physics)

__all__ = [
    'Physics', 'Body', 'BODY_BRUSH', 'BODY_PROPERTIES', 'WORLD_QUERIES',
    'SV_GRAVITY', 'HEAVY_MASS', 'CRUSH_DAMAGE', 'world_gravity',
    'DEFAULT_PHYSICS', 'register_physics', 'create_physics', 'select_physics',
    'selected_physics', 'available_physics', 'physics_factory',
]

#: Names that moved out of the old ``engine.physics`` module, and where to.
_MOVED = {
    'SpatialGrid': 'engine.spatial_grid',
    'Soma': 'engine.physics.soma',
    'PhysicsWorld': 'engine.physics.soma',
    'PhysicsBody': 'engine.physics.soma',
    'quat_from_rotation': 'engine.physics.soma',
    'rotation_from_quat': 'engine.physics.soma',
}


def __getattr__(name):
    home = _MOVED.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(home), name)
    globals()[name] = value
    return value
