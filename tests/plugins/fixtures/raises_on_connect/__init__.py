"""A plugin whose ``connect(host)`` raises after subscribing to one event.

Partial initialisation: the subscription it managed before failing is real, so
this is the case where a plugin is left half-wired into the host.
"""

from plugins.api import FioPlugin


class HalfConnectedPlugin(FioPlugin):
    name = "raises_on_connect"
    version = "1.0.0"
    description = "Subscribes to one event, then raises in connect()."

    def __init__(self):
        self.events_seen = []

    def register(self, api):
        pass

    def connect(self, host):
        host.on("test.ping", self._on_ping)
        raise RuntimeError("connect() failed on purpose, after subscribing")

    def _on_ping(self, event):
        self.events_seen.append(dict(event.data))


PLUGIN = HalfConnectedPlugin()
