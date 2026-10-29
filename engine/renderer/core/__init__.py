"""Optional renderer infrastructure.

CPU-only helpers (notably :mod:`.light_stream`) must be importable without
PyOpenGL. Loading :class:`RendererCore` pulls in the OpenGL-backed resource
mixins, so defer that import until the class itself is requested.
"""

from .diagnostics import timed_pass

__all__ = ["RendererCore", "timed_pass"]


def __getattr__(name):
    """Keep the public RendererCore export without eagerly importing OpenGL."""
    if name == "RendererCore":
        from .base import RendererCore

        globals()["RendererCore"] = RendererCore
        return RendererCore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
