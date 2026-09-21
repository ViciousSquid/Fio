"""A plugin that registers the same input handler and event twice.

Duplicate registration is a mistake a plugin author makes easily (registering
in both ``register`` and ``register_runtime``, say).  What matters is that the
host's behaviour is defined and does not compound: one handler wins, and an
event subscribed twice is not silently dropped.
"""

from plugins.api import FioPlugin


class DuplicateRegistrationPlugin(FioPlugin):
    name = "duplicate_handlers"
    version = "1.0.0"
    description = "Registers the same handler and event twice."

    def __init__(self):
        self.first_calls = 0
        self.second_calls = 0
        self.event_calls = 0

    def register(self, api):
        pass

    def register_runtime(self, api):
        api.register_input_handler("brush", "Twice", self._first)
        api.register_input_handler("brush", "Twice", self._second)

    def connect(self, host):
        host.on("test.dup", self._on_event)
        host.on("test.dup", self._on_event)

    def _first(self, entity, parameter, logic):
        self.first_calls += 1

    def _second(self, entity, parameter, logic):
        self.second_calls += 1

    def _on_event(self, event):
        self.event_calls += 1


PLUGIN = DuplicateRegistrationPlugin()
