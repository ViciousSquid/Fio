"""The physics registry: name -> factory.

``"Soma"`` is Fio's built-in physics. A plugin or developer adds another
with :func:`register_physics`; a play session creates the selected one with
:func:`create_physics`. A factory is any callable ``factory(spatial_grid)``
that returns an object satisfying :class:`engine.physics.api.Physics` --
usually the physics class itself.

Built-in entries are named by import path and resolved on first use, so
importing the registry (or the contract) needs neither NumPy nor PyGLM.
"""

import importlib

#: The physics a session starts with unless another is selected.
DEFAULT_PHYSICS = "Soma"

_factories = {
    "Soma": "engine.physics.soma:Soma",
}

_selected = DEFAULT_PHYSICS


def register_physics(name, factory):
    """Register *factory* under *name*, replacing any previous entry. Returns True."""
    _factories[str(name)] = factory
    return True


def available_physics():
    """The registered physics names."""
    return list(_factories)


def physics_factory(name):
    """The factory registered under *name*, or None."""
    factory = _factories.get(name)
    if isinstance(factory, str):
        module_name, _, attr = factory.partition(':')
        factory = getattr(importlib.import_module(module_name), attr)
        _factories[name] = factory
    return factory


def select_physics(name):
    """Make *name* the physics the next play session is created with.

    A running session keeps the one it has: bodies in motion are not handed
    between solvers. Raises KeyError for an unknown name.
    """
    global _selected
    if name not in _factories:
        raise KeyError(f"no physics registered as {name!r}")
    _selected = str(name)


def selected_physics():
    """The name :func:`create_physics` uses by default."""
    return _selected if _selected in _factories else DEFAULT_PHYSICS


def create_physics(spatial_grid, name=None):
    """Create the physics registered under *name* (default: the selected one)
    over *spatial_grid*, the session's populated grid.

    Raises KeyError for an unknown name.
    """
    if name is None:
        name = selected_physics()
    factory = physics_factory(name)
    if factory is None:
        raise KeyError(f"no physics registered as {name!r}")
    return factory(spatial_grid)
