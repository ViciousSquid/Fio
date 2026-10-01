"""A plugin using the API 1.5.0 editor content extensions.

It owns an entity type and supplies a property section for it, preset keys
for its own LogicState store, and the inspector document for its entities.
Every provider records its calls so a test can assert what it was handed.
"""

from plugins.api import FioPlugin
from plugins.entitybase import Thing


class GaugeEntity(Thing):
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties.setdefault("type", "gaugeentity")
        self.properties.setdefault("level", 3)


class EditorExtensionsPlugin(FioPlugin):
    name = "editor_extensions"
    version = "1.0.0"
    api_version = "1.5.0"
    description = "Property section, LogicState preset keys and an inspector."
    category = "Tests"
    enabled = True

    STORE = "gauges"

    def __init__(self):
        self.stores_seen = []
        self.inspected = []

    def register(self, api):
        api.register_entity(GaugeEntity, menu_label="Gauge")
        api.register_property_section("Calibration", self.make_section,
                                      entity_type="gaugeentity")
        api.register_kv_suggestions(self.suggest_keys)
        api.register_entity_inspector(self.inspect, entity_type="gaugeentity")

    def make_section(self, thing):
        return None

    def suggest_keys(self, store):
        self.stores_seen.append(store)
        props = getattr(store, "properties", {}) or {}
        if props.get("store_name") != self.STORE:
            return []
        return [("Gauge level", "gauge.level", "3", "The level every gauge starts at."),
                ("Gauge armed", "gauge.armed", "true")]

    def inspect(self, entity, logic):
        self.inspected.append((entity, logic))
        level = entity.properties.get("level", 0)
        return {"title": "Gauge", "subtitle": "calibrated",
                "sections": [("Readout", [("Level", level, level / 10.0)])]}


PLUGIN = EditorExtensionsPlugin()
