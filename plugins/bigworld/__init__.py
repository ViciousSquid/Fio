"""
Big World plugin package.

Exposes the module-level ``PLUGIN`` instance the plugin manager looks for.
"""

from .plugin import BigWorldPlugin

PLUGIN = BigWorldPlugin()

__all__ = ["PLUGIN", "BigWorldPlugin"]
