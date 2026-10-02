"""A plugin exposed through ``get_plugin()`` rather than a module-level PLUGIN.

Both forms are documented; this one covers the factory.
"""

from plugins.api import FioPlugin


class FactoryPlugin(FioPlugin):
    name = "factory_plugin"
    version = "1.0.0"
    description = "Built by get_plugin() at load time."

    def register(self, api):
        pass


_INSTANCE = None


def get_plugin():
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = FactoryPlugin()
    return _INSTANCE
