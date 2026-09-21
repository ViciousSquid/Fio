"""A package whose ``PLUGIN`` is the wrong type.

The manager must type-check rather than duck-type: calling ``register`` on
this would fail deep inside the load with a confusing traceback.
"""


class NotAFioPlugin:
    name = "not_a_plugin"
    version = "1.0.0"

    def register(self, api):
        raise AssertionError("this must never be called")


PLUGIN = NotAFioPlugin()
