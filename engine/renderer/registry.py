"""The renderer registry: name -> factory.

``"Forward"`` is Fio's built-in renderer. A plugin or developer adds another
with :func:`register_renderer`; the host creates the active one with
:func:`create_renderer`. A factory is any callable ``factory(config)`` that
returns an object satisfying :class:`engine.renderer.api.Renderer` -- usually
the renderer class itself.

Built-in entries are named by import path and resolved on first use, so
importing the registry (or the contract) needs no OpenGL.
"""

import importlib

#: The renderer the host starts with.
DEFAULT_RENDERER = "Forward"

_factories = {
    "Forward": "engine.renderer.forward:ForwardRenderer",
}


def register_renderer(name, factory):
    """Register *factory* under *name*, replacing any previous entry. Returns True."""
    _factories[str(name)] = factory
    return True


def available_renderers():
    """The registered renderer names."""
    return list(_factories)


def renderer_factory(name):
    """The factory registered under *name*, or None."""
    factory = _factories.get(name)
    if isinstance(factory, str):
        module_name, _, attr = factory.partition(':')
        factory = getattr(importlib.import_module(module_name), attr)
        _factories[name] = factory
    return factory


def create_renderer(name=DEFAULT_RENDERER, config=None):
    """Create the renderer registered under *name* (GL context current).

    Raises KeyError for an unknown name.
    """
    factory = renderer_factory(name)
    if factory is None:
        raise KeyError(f"no renderer registered as {name!r}")
    return factory(config)
