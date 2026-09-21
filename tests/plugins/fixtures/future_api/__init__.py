"""A plugin built against an API version this host does not provide.

Refusing it up front with a clear message is the contract; loading it and
failing later inside a hook is not.
"""

from plugins.api import FioPlugin


class FuturePlugin(FioPlugin):
    name = "future_api"
    version = "9.0.0"
    api_version = "99.0.0"
    description = "Needs a newer plugin API than this host has."

    def register(self, api):
        raise AssertionError("a plugin needing a newer API must never register")


PLUGIN = FuturePlugin()
