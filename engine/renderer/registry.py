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


class RendererContractError(TypeError):
    """A renderer factory's object does not satisfy the renderer contract:
    a member is missing or not callable, or it was written for a contract
    version this Fio cannot run (engine.renderer.api.contract_compatible)."""


class RendererInitError(RuntimeError):
    """A renderer could not be started: importing its factory or calling it
    raised (a missing library, a bug in its constructor -- a TypeError there
    included). The original exception is its ``__cause__``. Not a verdict
    on the contract."""

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

    What the factory returns is checked against the contract before the host
    paints with it: every member of :class:`~engine.renderer.api.Renderer`
    is there (``missing_members``), and it was not written for a newer or
    another major contract version (``contract_compatible``). One refused
    is cleaned up first, so the GL objects it made do not outlive it.

    Raises KeyError for an unknown name; :class:`RendererInitError` when the
    factory cannot be imported or raises; :class:`RendererContractError` for
    an object that does not satisfy the contract.
    """
    from .api import contract_compatible, missing_members
    if name not in _factories:
        raise KeyError(f"no renderer registered as {name!r}")
    try:
        factory = renderer_factory(name)
        renderer = factory(config)
    except Exception as exc:
        raise RendererInitError(
            f"renderer {name!r} failed to start: {type(exc).__name__}: {exc}") from exc
    missing = missing_members(renderer)
    reason = None if missing else contract_compatible(renderer)
    if missing or reason:
        cleanup = getattr(renderer, 'cleanup', None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass
        if missing:
            raise RendererContractError(
                f"renderer {name!r} does not satisfy the renderer contract: "
                f"missing or not callable: {', '.join(missing)}")
        raise RendererContractError(f"renderer {name!r} cannot run here: {reason}")
    return renderer
