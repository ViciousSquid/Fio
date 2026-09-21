"""
Fio plugin system.

A *plugin* is a Python sub-package of this ``plugins/`` directory that adds new
gameplay to Fio without touching the core editor or engine. Plugins can:

  * register new placeable entity types (``Thing`` subclasses),
  * register I/O definitions and runtime input handlers for those entities,
  * hook the play-mode lifecycle (start / tick / stop),
  * add entries to the editor's right-click "place entity" menu.

The public API a plugin builds against lives in :mod:`plugins.api`. Discovery
and dispatch are handled by :mod:`plugins.manager`. See ``plugins/README.md``
for a walkthrough and ``plugins/tidy`` for a complete worked example.
"""

from .api import FioPlugin, EditorAPI, RuntimeAPI, TickContext, io_def
from .manager import get_manager, load_plugins

__all__ = [
    "FioPlugin",
    "EditorAPI",
    "RuntimeAPI",
    "TickContext",
    "io_def",
    "get_manager",
    "load_plugins",
]
