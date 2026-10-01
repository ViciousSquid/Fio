"""Plugin API 1.5.0: property sections, LogicState preset keys, entity inspectors.

1.5.0 adds three editor *content* extensions: Fio owns the mechanism, a plugin
supplies the content. These tests pin the manager's side of each — what is
recorded, what a consumer is handed back, and that a disabled plugin's
contributions drop out — plus the version contract: 1.5.0 is additive, so a
plugin written against 1.4.0 loads and behaves unchanged, while one needing a
newer minor version is refused up front.

The Qt consumers (the Properties-tab sections, the LogicState "Preset key"
picker and the Entity Inspector panel) are covered in
``tests/editor/test_api_1_5_editor.py``.
"""

import pytest

from plugins.api import (API_VERSION, API_VERSION_INFO, EditorAPI, FioPlugin,
                         version_tuple)
from plugins.entitybase import Thing


def _thing(type_name, **props):
    props.setdefault("type", type_name)
    return Thing(pos=[1.0, 2.0, 3.0], properties=props)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

def test_the_host_api_is_1_5_0():
    assert API_VERSION == "1.5.0"
    assert API_VERSION_INFO == version_tuple(API_VERSION) == (1, 5, 0)


def test_the_editor_api_offers_the_1_5_methods():
    for name in ("register_property_section", "register_kv_suggestions",
                 "register_entity_inspector"):
        assert callable(getattr(EditorAPI, name, None)), name


def test_a_1_4_plugin_loads_under_1_5_with_its_registrations_intact(plugin_manager):
    manager = plugin_manager("api_1_4_plugin")
    plugin = manager.find_plugin("api_1_4_plugin")
    assert plugin is not None, "an API 1.4.0 plugin was refused by a 1.5.0 host"
    assert [label for label, _f in manager.property_tabs_for("api14entity")] == ["Legacy Tab"]
    assert [label for _p, label, _cb, _tip in manager.tools_actions()] == ["Legacy Tool"]
    assert manager.dispatch_console_command("legacy", "go") == (True, "legacy:go")
    assert plugin.commands == ["go"]


def test_a_1_4_plugin_contributes_nothing_to_the_1_5_extensions(plugin_manager):
    manager = plugin_manager("api_1_4_plugin")
    assert manager.property_sections_for("api14entity") == []
    assert manager.kv_suggestions(_thing("logic_state")) == []
    assert manager.inspect_entity(_thing("api14entity")) is None
    assert manager.has_entity_inspector("api14entity") is False


def test_a_plugin_needing_the_next_minor_api_is_refused(plugin_manager):
    manager = plugin_manager("next_minor_api", "editor_extensions")
    loaded = [p.name for p in manager.plugins]
    assert "next_minor_api" not in loaded
    assert "editor_extensions" in loaded


def test_a_1_5_plugin_loads(plugin_manager):
    manager = plugin_manager("editor_extensions")
    assert manager.find_plugin("editor_extensions") is not None


# ---------------------------------------------------------------------------
# Property sections
# ---------------------------------------------------------------------------

def test_a_property_section_applies_only_to_its_entity_type(plugin_manager):
    manager = plugin_manager("editor_extensions")
    plugin = manager.find_plugin("editor_extensions")
    assert manager.property_sections_for("gaugeentity") == [
        ("Calibration", plugin.make_section, False)]
    assert manager.property_sections_for("wellbehavedentity") == []


def test_a_property_section_matches_the_type_however_it_is_spelled(plugin_manager):
    manager = plugin_manager("editor_extensions")
    assert len(manager.property_sections_for("Gauge_Entity")) == 1


def test_an_untyped_section_applies_to_every_entity(plugin_manager):
    manager = plugin_manager()
    owner = FioPlugin()
    EditorAPI(manager, owner).register_property_section(
        "Everywhere", lambda thing: None, expanded=True)
    assert [(label, expanded) for label, _f, expanded
            in manager.property_sections_for("anything")] == [("Everywhere", True)]


def test_a_section_without_a_callable_factory_is_ignored(plugin_manager):
    manager = plugin_manager()
    EditorAPI(manager, FioPlugin()).register_property_section("Broken", None)
    assert manager.property_sections_for("anything") == []


# ---------------------------------------------------------------------------
# LogicState preset keys
# ---------------------------------------------------------------------------

def test_key_suggestions_are_offered_for_the_plugins_own_store(plugin_manager):
    manager = _fresh_extensions(plugin_manager)
    store = _thing("logic_state", store_name="gauges")
    assert manager.kv_suggestions(store) == [
        ("Gauge level", "gauge.level", "3", "The level every gauge starts at."),
        ("Gauge armed", "gauge.armed", "true", ""),
    ]
    assert manager.find_plugin("editor_extensions").stores_seen[-1] is store


def test_key_suggestions_can_decline_a_store(plugin_manager):
    manager = plugin_manager("editor_extensions")
    assert manager.kv_suggestions(_thing("logic_state", store_name="other")) == []


def test_key_suggestions_skip_bad_rows_and_duplicate_keys(plugin_manager):
    manager = plugin_manager()
    api = EditorAPI(manager, FioPlugin())
    api.register_kv_suggestions(lambda store: [
        ("A", "shared", 1),
        "not a row",
        ("too short",),
        ("Blank key", "  ", 0),
        ("B", "own", 2, "tip"),
    ])
    api.register_kv_suggestions(lambda store: [("A again", "shared", 9)])
    assert manager.kv_suggestions(None) == [("A", "shared", 1, ""),
                                            ("B", "own", 2, "tip")]


def test_a_failing_key_provider_is_logged_and_the_rest_still_answer(plugin_manager):
    manager = plugin_manager()
    api = EditorAPI(manager, FioPlugin())
    logged = []
    manager._log = logged.append

    def broken(store):
        raise RuntimeError("provider bug")

    api.register_kv_suggestions(broken)
    api.register_kv_suggestions(lambda store: [("Fine", "fine", 1)])
    assert manager.kv_suggestions(None) == [("Fine", "fine", 1, "")]
    assert any("provider bug" in line for line in logged)


# ---------------------------------------------------------------------------
# Entity inspectors
# ---------------------------------------------------------------------------

def _fresh_extensions(plugin_manager):
    """The fixture plugin's manager, with its call logs emptied.

    The fixture module (and so its ``PLUGIN``) is imported once per session,
    so its logs would otherwise carry calls over from earlier tests.
    """
    manager = plugin_manager("editor_extensions")
    plugin = manager.find_plugin("editor_extensions")
    plugin.inspected.clear()
    plugin.stores_seen.clear()
    return manager


def test_the_inspector_provider_describes_its_entities(plugin_manager):
    manager = _fresh_extensions(plugin_manager)
    gauge = _thing("gaugeentity", level=4)
    logic = object()
    document = manager.inspect_entity(gauge, logic)
    assert document["title"] == "Gauge"
    assert document["sections"] == [("Readout", [("Level", 4, 0.4)])]
    assert manager.find_plugin("editor_extensions").inspected == [(gauge, logic)]


def test_a_typed_inspector_is_not_asked_about_other_entities(plugin_manager):
    manager = _fresh_extensions(plugin_manager)
    assert manager.inspect_entity(_thing("wellbehavedentity")) is None
    assert manager.find_plugin("editor_extensions").inspected == []
    assert manager.has_entity_inspector("gaugeentity") is True
    assert manager.has_entity_inspector("wellbehavedentity") is False


def test_the_first_provider_with_a_document_wins(plugin_manager):
    manager = plugin_manager()
    api = EditorAPI(manager, FioPlugin())
    api.register_entity_inspector(lambda entity, logic: None)
    api.register_entity_inspector(lambda entity, logic: {"title": "second"})
    api.register_entity_inspector(lambda entity, logic: {"title": "third"})
    assert manager.inspect_entity(_thing("anything")) == {"title": "second"}


def test_a_failing_inspector_is_logged_and_the_next_one_asked(plugin_manager):
    manager = plugin_manager()
    api = EditorAPI(manager, FioPlugin())
    logged = []
    manager._log = logged.append

    def broken(entity, logic):
        raise RuntimeError("inspector bug")

    api.register_entity_inspector(broken)
    api.register_entity_inspector(lambda entity, logic: {"title": "ok"})
    assert manager.inspect_entity(_thing("anything")) == {"title": "ok"}
    assert any("inspector bug" in line for line in logged)


# ---------------------------------------------------------------------------
# Enabled state
# ---------------------------------------------------------------------------

def test_a_disabled_plugins_extensions_drop_out_and_return(plugin_manager):
    manager = plugin_manager("editor_extensions")
    plugin = manager.find_plugin("editor_extensions")
    store = _thing("logic_state", store_name="gauges")
    gauge = _thing("gaugeentity")

    manager.set_enabled(plugin, False)
    assert manager.property_sections_for("gaugeentity") == []
    assert manager.kv_suggestions(store) == []
    assert manager.inspect_entity(gauge) is None
    assert manager.has_entity_inspector("gaugeentity") is False

    manager.set_enabled(plugin, True)
    assert len(manager.property_sections_for("gaugeentity")) == 1
    assert len(manager.kv_suggestions(store)) == 2
    assert manager.inspect_entity(gauge)["title"] == "Gauge"


def test_a_builtin_game_layer_can_use_the_extensions(plugin_manager):
    """A built-in game layer has no ``enabled`` switch and is always on."""
    manager = plugin_manager()

    class Layer:
        name = "layer"

        def register(self, api):
            api.register_property_section("Layer", lambda thing: None)
            api.register_kv_suggestions(lambda store: [("L", "layer.key", 1)])
            api.register_entity_inspector(lambda entity, logic: {"title": "L"})

    manager.register_builtin_game(Layer())
    assert [label for label, _f, _e in manager.property_sections_for("x")] == ["Layer"]
    assert manager.kv_suggestions(None) == [("L", "layer.key", 1, "")]
    assert manager.inspect_entity(_thing("x")) == {"title": "L"}

