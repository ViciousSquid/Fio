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
built-in physics: Soma. The world is not physics' to hold: where its brushes, movers,
doors and water are is :class:`engine.spatial_grid.SpatialGrid`'s to say --
engine core, which a physics is created with and asks. Importing this package needs neither NumPy nor PyGLM.

This package exports the boundary only. ``SpatialGrid`` is imported from
:mod:`engine.spatial_grid`; Soma's own classes (``Soma``, ``PhysicsBody``)
and helpers from :mod:`engine.physics.soma`.
"""

from .api import (
    BODY_BRUSH, BODY_PROPERTIES, CRUSH_DAMAGE, HEAVY_MASS, NO_PHYSICS,
    PHYSICS_EVENTS, SV_GRAVITY, WORLD_QUERIES, Body, NoPhysics, Physics,
    world_gravity)
from .registry import (
    DEFAULT_PHYSICS, available_physics, create_physics, physics_factory,
    register_physics, select_physics, selected_physics, unload_physics)

__all__ = [
    'Physics', 'Body', 'BODY_BRUSH', 'BODY_PROPERTIES', 'WORLD_QUERIES',
    'PHYSICS_EVENTS', 'NoPhysics', 'NO_PHYSICS',
    'SV_GRAVITY', 'HEAVY_MASS', 'CRUSH_DAMAGE', 'world_gravity',
    'DEFAULT_PHYSICS', 'register_physics', 'create_physics', 'select_physics',
    'selected_physics', 'available_physics', 'physics_factory',
    'unload_physics',
]
