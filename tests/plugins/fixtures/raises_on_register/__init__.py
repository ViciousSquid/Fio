"""A plugin whose ``register()`` raises.

The host must log it and carry on: a broken plugin may not stop the editor
starting, and must not end up in the loaded list half-registered.
"""

from plugins.api import FioPlugin


class ExplodingPlugin(FioPlugin):
    name = "raises_on_register"
    version = "1.0.0"
    description = "Raises during register() on purpose."

    def register(self, api):
        raise RuntimeError("register() failed on purpose")


PLUGIN = ExplodingPlugin()
