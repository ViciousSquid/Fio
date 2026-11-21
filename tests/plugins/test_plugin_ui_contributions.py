"""A plugin's editor UI goes when the plugin does (audit S18).

Property tabs and extra fields were recorded without their owner, so a
disabled plugin -- or one whose ``register()`` raised halfway, which never
joins the loaded list and so cannot be switched off -- still put its tabs and
fields on every panel and ran its factories. Each contribution now carries
its plugin; the read side asks whether that plugin is on, and a failed
registration takes back everything it had recorded.
"""

from plugins.api import EditorAPI, FioPlugin, PropertySpec
from plugins.entitybase import Thing


def test_a_disabled_plugins_property_tab_drops_out(plugin_manager):
    manager = plugin_manager("api_1_4_plugin")
    plugin = manager.find_plugin("api_1_4_plugin")
    assert [label for label, _f in manager.property_tabs_for("api14entity")] == ["Legacy Tab"]
    manager.set_enabled(plugin, False)
    assert manager.property_tabs_for("api14entity") == []
    manager.set_enabled(plugin, True)
    assert [label for label, _f in manager.property_tabs_for("api14entity")] == ["Legacy Tab"]


def test_a_disabled_plugins_extra_fields_drop_out(plugin_manager):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    EditorAPI(manager, plugin).register_extra_fields(
        "prop", [PropertySpec("glow", "float", label="Glow")])
    assert [s.name for s in manager.extra_fields_for("prop")] == ["glow"]
    manager.set_enabled(plugin, False)
    assert manager.extra_fields_for("prop") == []


def test_host_registered_tabs_and_fields_have_no_plugin_to_switch_off(plugin_manager):
    manager = plugin_manager("well_behaved")
    manager.register_property_tab("Host Tab", lambda thing: None)
    manager.register_extra_fields("prop", [PropertySpec("host", "bool")])
    assert [label for label, _f in manager.property_tabs_for("prop")] == ["Host Tab"]
    assert [s.name for s in manager.extra_fields_for("prop")] == ["host"]


def test_a_register_that_raises_halfway_leaves_no_editor_ui(plugin_manager):
    manager = plugin_manager("raises_midway_register", "well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"]
    assert manager.property_tabs_for("prop") == []
    assert manager.extra_fields_for("prop") == []
    assert manager.property_sections_for("prop") == []
    store = Thing(pos=[0, 0, 0], properties={"type": "logic_state"})
    assert manager.kv_suggestions(store) == []
    assert manager.inspect_entity(Thing(pos=[0, 0, 0], properties={"type": "prop"})) is None
    # Nor are the registries holding the half-registered plugin's entries
    # (a later enable must not bring them back).
    import tests.plugins.fixtures.raises_midway_register as module
    half = module.PLUGIN
    manager.set_enabled(half, True)
    assert manager.property_tabs_for("prop") == []
    assert manager.extra_fields_for("prop") == []
    assert manager.property_sections_for("prop") == []
    assert half.factory_calls == []
