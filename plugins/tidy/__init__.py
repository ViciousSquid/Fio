"""
Tidy plugin package.

Exposes the module-level ``PLUGIN`` instance the plugin manager looks for.
"""

from .plugin import TidyPlugin

PLUGIN = TidyPlugin()

__all__ = ["PLUGIN", "TidyPlugin"]
