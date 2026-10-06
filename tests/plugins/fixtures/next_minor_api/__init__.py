"""A plugin that needs the next minor API version (1.7.0).

A 1.6.0 host must refuse it up front, exactly as it refuses a far-future one.
"""

from plugins.api import FioPlugin


class NextMinorPlugin(FioPlugin):
    name = "next_minor_api"
    version = "1.0.0"
    api_version = "1.7.0"
    description = "Needs a newer minor plugin API than this host has."

    def register(self, api):
        raise AssertionError("a plugin needing a newer API must never register")


PLUGIN = NextMinorPlugin()
