"""A plugin that does everything right, and records that it was asked to.

The reference against which the failing plugins in this directory are read: it
registers an entity type and its I/O, subscribes to an event, takes part in the
play lifecycle and the per-frame tick, and keeps a log of every callback so a
test can assert on the exact sequence rather than on a flag.
"""

from plugins.api import FioPlugin, io_def, prop
from plugins.entitybase import Thing


class WellBehavedEntity(Thing):
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault("type", "wellbehavedentity")
        self.properties.setdefault("charge", 1)


class WellBehavedPlugin(FioPlugin):
    name = "well_behaved"
    version = "1.0.0"
    description = "Exercises the whole plugin contract and logs every callback."
    category = "Tests"
    enabled = True

    def __init__(self):
        #: Every lifecycle callback, in order, as ``(hook, detail)`` pairs.
        self.calls = []
        self.events_seen = []
        self.api = None
        self.runtime = None
        self.host = None
        self.tick_count = 0

    # -- load time --------------------------------------------------------
    def register(self, api):
        self.api = api
        self.calls.append(("register", None))
        api.register_entity(WellBehavedEntity, menu_label="Well Behaved")
        api.register_io("wellbehavedentity",
                        inputs=[io_def("Charge", "add one charge")],
                        outputs=[io_def("OnCharged", "charge added")])

    def describe_properties(self):
        return {"wellbehavedentity": [prop("charge", "int", "Charge", 1)]}

    # -- runtime ----------------------------------------------------------
    def register_runtime(self, api):
        self.runtime = api
        self.calls.append(("register_runtime", None))
        api.register_input_handler("wellbehavedentity", "Charge", self._on_charge)

    def _on_charge(self, entity, parameter, logic):
        entity.properties["charge"] = int(entity.properties.get("charge", 0)) + 1
        self.calls.append(("Charge", entity.properties.get("name")))

    def connect(self, host):
        self.host = host
        self.calls.append(("connect", None))
        host.on("test.ping", self._on_ping)
        host.provide("well_behaved_service", self)

    def _on_ping(self, event):
        self.events_seen.append(dict(event.data))

    # -- play lifecycle ---------------------------------------------------
    def on_play_start(self, logic):
        self.calls.append(("on_play_start", None))

    def on_play_stop(self, logic):
        self.calls.append(("on_play_stop", None))

    def on_tick(self, logic, ctx):
        self.tick_count += 1

    def on_enabled_changed(self, enabled):
        self.calls.append(("on_enabled_changed", enabled))

    def menu_entries(self):
        return [("Well Behaved (menu)", WellBehavedEntity)]


PLUGIN = WellBehavedPlugin()
