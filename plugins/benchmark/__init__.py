"""Optional developer Benchmark plugin.

Disabled by default so normal Fio sessions have no benchmark UI, manager process,
or benchmark-specific imports.
"""

from .plugin import BenchmarkPlugin

PLUGIN = BenchmarkPlugin()

__all__ = ["PLUGIN", "BenchmarkPlugin"]
