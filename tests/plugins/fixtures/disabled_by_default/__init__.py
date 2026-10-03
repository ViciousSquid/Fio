"""A plugin that ships switched off, like Fio's own Tidy plugin.

It stays inert until a level that uses its entity type is loaded, at which
point the manager auto-enables it.
"""

from plugins.api import FioPlugin
from plugins.entitybase import Thing


class SleepingEntity(Thing):
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault("type", "sleepingentity")


class DisabledByDefaultPlugin(FioPlugin):
    name = "disabled_by_default"
    version = "1.0.0"
    description = "Off until a level references its entity."
    enabled = False

    def __init__(self):
        self.ticks = 0

    def register(self, api):
        api.register_entity(SleepingEntity, menu_label="Sleeping Entity")

    def on_tick(self, logic, ctx):
        self.ticks += 1


PLUGIN = DisabledByDefaultPlugin()
