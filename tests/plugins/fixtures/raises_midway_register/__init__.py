"""A plugin whose ``register()`` records editor UI and then raises.

Everything it recorded before the exception must be taken back: it never
joins the loaded list, so nothing could switch its contributions off.
"""

from plugins.api import FioPlugin, PropertySpec


class HalfRegisteredPlugin(FioPlugin):
    name = "raises_midway_register"
    version = "1.0.0"
    api_version = "1.5.0"
    description = "Registers editor UI, then raises in register() on purpose."
    category = "Tests"

    def __init__(self):
        self.factory_calls = []

    def register(self, api):
        api.register_property_tab("Half Tab", self._factory)
        api.register_extra_fields("prop", [PropertySpec("half_field", "string", label="Half")])
        api.register_property_section("Half Section", self._factory)
        api.register_kv_suggestions(lambda store: [("Half", "half.key", "1")])
        api.register_entity_inspector(lambda entity, logic: {"title": "Half"})
        raise RuntimeError("register() failed halfway on purpose")

    def _factory(self, thing):
        self.factory_calls.append(thing)
        return None


PLUGIN = HalfRegisteredPlugin()
