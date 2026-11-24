"""The renderer registry: name -> factory.

``"Forward"`` is Fio's built-in renderer. A plugin or developer adds another
with :func:`register_renderer`; the host creates the active one with
:func:`create_renderer`. A factory is any callable ``factory(config)`` that
returns an object satisfying :class:`engine.renderer.api.Renderer` -- usually
the renderer class itself. :func:`create_renderer` checks that it does,
whichever renderer it is: the built-in ones are held to the contract exactly
as a plugin's is.

Built-in entries are named by import path and resolved on first use, so
importing the registry (or the contract) needs no OpenGL.
"""

import importlib


class RendererContractError(TypeError):
    """A renderer factory's object does not satisfy the renderer contract
    (engine.renderer.api.contract_violations): a member is missing or cannot
    be read, a method does not take the contract's arguments, an attribute
    holds a value of the wrong type, or it does not declare a contract
    version this Fio can run."""


class RendererInitError(RuntimeError):
    """A renderer could not be started: importing its factory or calling it
    raised (a missing library, a bug in its constructor -- a TypeError there
    included). The original exception is its ``__cause__``. Not a verdict
    on the contract."""

#: The renderer the host starts with: Fio's original forward renderer, the
#: one people call Classic.
DEFAULT_RENDERER = "Forward"

#: Other names a renderer answers to. "Classic" is what the forward renderer
#: has been called since there was a second one.
ALIASES = {"Classic": "Forward"}

_factories = {
    "Forward": "engine.renderer.forward:ForwardRenderer",
    # A separate renderer of its own package: physically based surfaces, a
    # sun and sky, HDR post-processing (engine/renderer/hd_high).
    "HD_High": "engine.renderer.hd_high:HDHighRenderer",
}


def canonical_name(name):
    """The registry key *name* stands for (an alias resolved; anything else
    as given)."""
    return ALIASES.get(name, name)


def register_renderer(name, factory):
    """Register *factory* under *name*, replacing any previous entry. Returns True."""
    _factories[str(name)] = factory
    return True


def available_renderers():
    """The registered renderer names."""
    return list(_factories)


def renderer_factory(name):
    """The factory registered under *name*, or None."""
    name = canonical_name(name)
    factory = _factories.get(name)
    if isinstance(factory, str):
        module_name, _, attr = factory.partition(':')
        factory = getattr(importlib.import_module(module_name), attr)
        _factories[name] = factory
    return factory


def _retire(renderer):
    """Clean up a renderer the registry refused. Returns the exception its
    ``cleanup`` raised, or None."""
    try:
        renderer.cleanup()
    except Exception as exc:
        return exc
    return None


def create_renderer(name=DEFAULT_RENDERER, config=None):
    """Create the renderer registered under *name* (GL context current).

    What the factory returns is checked against the contract before the host
    paints with it (:func:`~engine.renderer.api.contract_violations`): every
    member of :class:`~engine.renderer.api.Renderer` is there and can be
    read, each method takes the arguments the contract declares, each
    attribute holds a value of its declared type, and it declares a
    ``contract_version`` this Fio can run. One refused is cleaned up first,
    so the GL objects it made do not outlive it.

    Raises KeyError for an unknown name; :class:`RendererInitError` when the
    factory cannot be imported or raises; :class:`RendererContractError` for
    an object that does not satisfy the contract, naming every violation.
    """
    from .api import contract_violations
    name = canonical_name(name)
    if name not in _factories:
        raise KeyError(f"no renderer registered as {name!r}")
    try:
        factory = renderer_factory(name)
        renderer = factory(config)
    except Exception as exc:
        raise RendererInitError(
            f"renderer {name!r} failed to start: {type(exc).__name__}: {exc}") from exc
    violations = contract_violations(renderer)
    if violations:
        error = RendererContractError(
            f"renderer {name!r} does not satisfy the renderer contract: "
            + "; ".join(violations))
        failed = _retire(renderer)
        if failed is not None:
            error.add_note(
                f"its cleanup() raised {type(failed).__name__}: {failed}; "
                "GL objects it made may remain")
        raise error
    return renderer
