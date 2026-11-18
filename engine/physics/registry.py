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


class PhysicsContractError(TypeError):
    """A physics factory's object does not satisfy the physics contract:
    a member is missing or not callable, or it was written for a contract
    version this Fio cannot run (engine.physics.api.contract_compatible).
    A TypeError, as the registry raised before this class existed."""


class PhysicsInitError(RuntimeError):
    """A physics could not be started: importing its factory or calling it
    raised (a missing native library, an ImportError, a bug in its
    constructor -- a TypeError there included). The original exception is
    its ``__cause__``. Not a verdict on the contract."""

#: The physics a session starts with unless another is selected.
DEFAULT_PHYSICS = "Soma"

#: The physics Fio ships, by where each lives.
_BUILT_IN = {
    "Soma": "engine.physics.soma:Soma",
}

_factories = dict(_BUILT_IN)

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

    What the factory returns is checked against the contract before the
    engine drives it: every member of :class:`~engine.physics.api.Physics`
    is there (``missing_members``), and it was not written for a newer or
    another major contract version (``contract_compatible``).

    Raises KeyError for an unknown name; :class:`PhysicsInitError` when the
    factory cannot be imported or raises; :class:`PhysicsContractError` for
    an object that does not satisfy the contract.
    """
    from .api import contract_compatible, missing_members
    if name is None:
        name = selected_physics()
    if name not in _factories:
        raise KeyError(f"no physics registered as {name!r}")
    try:
        factory = physics_factory(name)
        physics = factory(spatial_grid)
    except Exception as exc:
        raise PhysicsInitError(
            f"physics {name!r} failed to start: {type(exc).__name__}: {exc}") from exc
    missing = missing_members(physics)
    if missing:
        raise PhysicsContractError(
            f"physics {name!r} does not satisfy the physics contract: "
            f"missing or not callable: {', '.join(missing)}")
    reason = contract_compatible(physics)
    if reason is not None:
        raise PhysicsContractError(f"physics {name!r} cannot run here: {reason}")
    return physics


def unload_physics():
    """Unload the built-in physics: the implementation modules leave
    ``sys.modules`` and are imported again only if a physics is next created.

    For a host that has physics switched off: nothing of Soma stays in memory.
    The boundary (this registry, the contract) stays; it is stdlib-only and a
    few kilobytes. Returns the names of the modules dropped. A physics some
    session still holds keeps its own module alive until that session ends.
    """
    import sys
    dropped = []
    for name, target in _BUILT_IN.items():
        module_name = target.partition(':')[0]
        factory = _factories.get(name)
        if not isinstance(factory, str) and getattr(factory, '__module__', '').startswith(module_name):
            _factories[name] = target
        for loaded in [m for m in sys.modules
                       if m == module_name or m.startswith(module_name + '.')]:
            del sys.modules[loaded]
            dropped.append(loaded)
    # The import system set each dropped submodule as an attribute of its
    # package (``engine.physics.soma``): take it off too, or the package
    # would keep the module alive.
    for loaded in dropped:
        parent, _dot, child = loaded.rpartition('.')
        package = sys.modules.get(parent)
        if package is not None and getattr(package, child, None) is not None:
            vars(package).pop(child, None)
    return dropped
