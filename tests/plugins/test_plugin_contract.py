"""The plugin API as a contract, tested the way a plugin author uses it.

Sections covered: discovery, loading, metadata, registration, event
registration and dispatch, editor and runtime hooks, lifecycle callbacks, API
helpers, isolation and failure handling.

The guiding rule for the failure cases is the one the plugin system documents:
*a misbehaving plugin logs an error instead of taking down the editor or a play
session.*  So every "broken plugin" test asserts two things — the host survived,
and the other plugins still ran.
"""

import pytest

from plugins.api import API_VERSION, EditorAPI, FioPlugin, version_tuple
from plugins.host import Event, EventBus

pytestmark = []


# ---------------------------------------------------------------------------
# Discovery and loading
# ---------------------------------------------------------------------------

def test_a_well_formed_plugin_is_discovered_and_registered(plugin_manager):
    manager = plugin_manager("well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"]
    plugin = manager.find_plugin("well_behaved")
    assert ("register", None) in plugin.calls, (
        "register() was never called; the plugin's callback log is %s"
        % (plugin.calls,))


def test_a_plugin_exposed_through_get_plugin_is_loaded(plugin_manager):
    manager = plugin_manager("factory_plugin")
    assert [p.name for p in manager.plugins] == ["factory_plugin"], (
        "a package exposing get_plugin() instead of PLUGIN was not loaded")


def test_a_package_with_no_plugin_is_skipped(plugin_manager):
    manager = plugin_manager("no_plugin_attribute", "well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"], (
        "a package exposing neither PLUGIN nor get_plugin() must be skipped, "
        "and the good plugin beside it must still load; loaded %s"
        % ([p.name for p in manager.plugins],))


def test_a_plugin_object_of_the_wrong_type_is_refused(plugin_manager):
    manager = plugin_manager("not_a_plugin", "well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"], (
        "PLUGIN must be type-checked against FioPlugin; loaded %s"
        % ([p.name for p in manager.plugins],))


def test_a_plugin_needing_a_newer_api_is_refused_up_front(plugin_manager):
    manager = plugin_manager("future_api", "well_behaved")
    assert "future_api" not in [p.name for p in manager.plugins], (
        "a plugin declaring api_version 99.0.0 was loaded against host API %s"
        % API_VERSION)
    assert "well_behaved" in [p.name for p in manager.plugins]


def test_a_plugin_whose_register_raises_is_not_loaded(plugin_manager):
    manager = plugin_manager("raises_on_register", "well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"], (
        "a plugin that raised in register() must not end up half-registered "
        "in the loaded list; loaded %s" % ([p.name for p in manager.plugins],))


def test_a_package_that_cannot_be_imported_is_skipped(plugin_manager):
    manager = plugin_manager("no_such_package_at_all", "well_behaved")
    assert [p.name for p in manager.plugins] == ["well_behaved"], (
        "an unimportable package must be logged and skipped, not raised")


def test_discovery_is_idempotent(plugin_manager):
    manager = plugin_manager("well_behaved")
    before = list(manager.plugins)
    manager.discover_and_load()
    manager.discover_and_load()
    assert manager.plugins == before, (
        "repeat discovery registered plugins twice: %s"
        % ([p.name for p in manager.plugins],))


def test_fio_disabled_plugins_skips_a_named_package_entirely(plugin_manager,
                                                             monkeypatch):
    """The env-var kill switch: the package is never even imported."""
    monkeypatch.setenv("FIO_DISABLED_PLUGINS", "well_behaved")
    manager = plugin_manager("well_behaved", "factory_plugin")
    assert [p.name for p in manager.plugins] == ["factory_plugin"], (
        "FIO_DISABLED_PLUGINS=well_behaved should leave only factory_plugin "
        "loaded; got %s" % ([p.name for p in manager.plugins],))


def test_the_disabled_list_is_matched_case_insensitively(plugin_manager,
                                                         monkeypatch):
    monkeypatch.setenv("FIO_DISABLED_PLUGINS", "WELL_BEHAVED , factory_plugin")
    manager = plugin_manager("well_behaved", "factory_plugin")
    assert manager.plugins == [], (
        "a comma-separated, differently-cased disabled list left %s loaded"
        % ([p.name for p in manager.plugins],))


def test_all_the_fixture_plugins_load_together_without_the_broken_ones(
        plugin_manager):
    """The whole directory at once: the good ones load, the bad ones do not."""
    manager = plugin_manager()
    loaded = sorted(p.name for p in manager.plugins)
    assert "well_behaved" in loaded
    assert "factory_plugin" in loaded
    for broken in ("raises_on_register", "not_a_plugin", "future_api",
                   "no_plugin_attribute"):
        assert broken not in loaded, (
            "%s should not have loaded; loaded set is %s" % (broken, loaded))


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_a_plugin_reports_its_documented_metadata(plugin_manager):
    plugin = plugin_manager("well_behaved").find_plugin("well_behaved")
    assert plugin.name == "well_behaved"
    assert plugin.version == "1.0.0"
    assert plugin.description
    assert plugin.category == "Tests"


def test_find_plugin_is_case_insensitive(plugin_manager):
    manager = plugin_manager("well_behaved")
    assert manager.find_plugin("WELL_BEHAVED") is manager.find_plugin("well_behaved")


def test_find_plugin_returns_none_for_an_unknown_name(plugin_manager):
    assert plugin_manager("well_behaved").find_plugin("nope") is None


def test_has_plugins_reflects_what_loaded(plugin_manager):
    assert plugin_manager("well_behaved").has_plugins() is True
    assert plugin_manager("not_a_plugin").has_plugins() is False


@pytest.mark.parametrize("text,expected", [
    ("1.0.0", (1, 0, 0)),
    ("1.3", (1, 3)),
    ("2.10.1", (2, 10, 1)),
])
def test_version_tuple_parses_a_version_string(text, expected):
    assert version_tuple(text) == expected


def test_the_host_api_version_is_at_least_what_the_shipped_plugins_need():
    from plugins.bigworld import PLUGIN as bigworld
    from plugins.tidy import PLUGIN as tidy
    for plugin in (bigworld, tidy):
        needs = getattr(plugin, "api_version", "1.0.0")
        assert version_tuple(needs) <= version_tuple(API_VERSION), (
            "shipped plugin '%s' needs API %s but the host provides %s"
            % (plugin.name, needs, API_VERSION))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registering_an_entity_records_its_owner(plugin_manager):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    assert manager.plugin_for_type("wellbehavedentity") is plugin, (
        "the entity's owning plugin was not recorded; owners are %s"
        % ({k: v.name for k, v in manager._entity_owner.items()},))


def test_registering_an_entity_makes_its_class_resolvable_by_type(plugin_manager):
    manager = plugin_manager("well_behaved")
    from tests.plugins.fixtures.well_behaved import WellBehavedEntity
    assert manager.entity_class_for_type("wellbehavedentity") is WellBehavedEntity


def test_entity_type_lookup_normalises_case_and_underscores(plugin_manager):
    manager = plugin_manager("well_behaved")
    for spelling in ("WellBehavedEntity", "well_behaved_entity", "WELLBEHAVEDENTITY"):
        assert manager.entity_class_for_type(spelling) is not None, (
            "type lookup failed for the spelling %r" % spelling)


def test_a_registered_entity_appears_in_the_menu_entries(plugin_manager):
    manager = plugin_manager("well_behaved")
    labels = [label for _plugin, label, _cls in manager.menu_entries()]
    assert "Well Behaved" in labels, (
        "the registered entity is missing from the placement menu: %s" % (labels,))
    assert "Well Behaved (menu)" in labels, (
        "the plugin's own menu_entries() were not collected: %s" % (labels,))


def test_a_declared_property_schema_is_recorded(plugin_manager):
    manager = plugin_manager("well_behaved")
    schema = manager.property_schema_for("wellbehavedentity")
    assert schema is not None, "describe_properties() was not collected"
    assert [spec.name for spec in schema] == ["charge"]


def test_registering_io_definitions_reaches_the_io_registry(plugin_manager):
    pytest.importorskip("PyQt5", reason="the I/O registry lives in the editor package")
    from editor import io_system
    plugin_manager("well_behaved")
    assert io_system.get_input_names("wellbehavedentity") == ["Charge"]
    assert io_system.get_output_names("wellbehavedentity") == ["OnCharged"]


def test_required_plugins_for_types_names_the_owner(plugin_manager):
    manager = plugin_manager("disabled_by_default")
    required = manager.required_plugins_for_types(["sleepingentity"])
    assert [p.name for p in required] == ["disabled_by_default"]


def test_required_plugins_for_an_unowned_type_is_empty(plugin_manager):
    manager = plugin_manager("well_behaved")
    assert manager.required_plugins_for_types(["light"]) == []


# ---------------------------------------------------------------------------
# Enabling and auto-enabling
# ---------------------------------------------------------------------------

def test_a_plugin_can_ship_switched_off(plugin_manager):
    manager = plugin_manager("disabled_by_default")
    plugin = manager.find_plugin("disabled_by_default")
    assert manager.is_enabled(plugin) is False, (
        "a plugin declaring enabled = False should start off")


def test_loading_a_map_that_uses_a_plugins_entity_switches_it_on(plugin_manager):
    manager = plugin_manager("disabled_by_default")
    plugin = manager.find_plugin("disabled_by_default")

    newly = manager.auto_enable_for_map({
        "things": [{"type": "sleepingentity", "pos": [0, 0, 0]}]})

    assert [p.name for p in newly] == ["disabled_by_default"]
    assert manager.is_enabled(plugin) is True


def test_a_map_that_uses_nothing_of_the_plugins_leaves_it_off(plugin_manager):
    manager = plugin_manager("disabled_by_default")
    newly = manager.auto_enable_for_map({"things": [{"type": "light"}]})
    assert newly == []
    assert manager.is_enabled(manager.find_plugin("disabled_by_default")) is False


def test_clearing_the_scene_reverts_only_the_auto_enabled_plugins(plugin_manager):
    manager = plugin_manager("disabled_by_default", "well_behaved")
    auto = manager.find_plugin("disabled_by_default")
    manual = manager.find_plugin("well_behaved")
    manager.set_enabled(manual, True)             # a deliberate menu toggle
    manager.auto_enable_for_types(["sleepingentity"])

    reverted = manager.disable_auto_enabled()

    assert [p.name for p in reverted] == ["disabled_by_default"]
    assert manager.is_enabled(auto) is False
    assert manager.is_enabled(manual) is True, (
        "a plugin the user enabled by hand was switched off underneath them")


def test_toggling_a_plugin_calls_its_on_enabled_changed_hook(plugin_manager):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    plugin.calls.clear()

    manager.set_enabled(plugin, False)
    manager.set_enabled(plugin, True)

    assert [call for call in plugin.calls if call[0] == "on_enabled_changed"] == [
        ("on_enabled_changed", False), ("on_enabled_changed", True)], (
        "the enable/disable hook was not called in order; log is %s"
        % (plugin.calls,))


def test_setting_the_state_it_already_has_does_not_re_notify(plugin_manager):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    manager.set_enabled(plugin, True)
    plugin.calls.clear()
    manager.set_enabled(plugin, True)
    assert plugin.calls == [], (
        "re-enabling an already-enabled plugin notified it: %s" % (plugin.calls,))


# ---------------------------------------------------------------------------
# The event bus
# ---------------------------------------------------------------------------

def test_a_handler_receives_the_event_payload():
    bus = EventBus()
    seen = []
    bus.on("thing.happened", lambda event: seen.append(event.data))
    bus.emit("thing.happened", who="player", amount=3)
    assert seen == [{"who": "player", "amount": 3}]


def test_emitting_an_event_nobody_listens_to_returns_none():
    assert EventBus().emit("nobody.listening", x=1) is None


def test_handlers_run_highest_priority_first_then_in_subscription_order():
    bus = EventBus()
    order = []
    bus.on("e", lambda ev: order.append("low"), priority=0)
    bus.on("e", lambda ev: order.append("high"), priority=10)
    bus.on("e", lambda ev: order.append("low2"), priority=0)
    bus.emit("e")
    assert order == ["high", "low", "low2"], (
        "dispatch order was %s; priority first, then subscription order" % (order,))


def test_a_handler_can_stop_the_event():
    bus = EventBus()
    order = []

    def _stopper(event):
        order.append("first")
        event.stop()

    bus.on("e", _stopper, priority=10)
    bus.on("e", lambda ev: order.append("second"))
    bus.emit("e")
    assert order == ["first"], "stop() did not halt dispatch: %s" % (order,)


def test_a_handler_that_raises_does_not_stop_the_others():
    bus = EventBus()
    order = []

    def _boom(event):
        raise RuntimeError("handler failed on purpose")

    bus.on("e", _boom, priority=10)
    bus.on("e", lambda ev: order.append("after"))
    bus.emit("e")
    assert order == ["after"], (
        "a raising handler swallowed the subscribers after it")


def test_unsubscribing_stops_delivery():
    bus = EventBus()
    seen = []
    handler = bus.on("e", lambda ev: seen.append(1))
    bus.off("e", handler)
    bus.emit("e")
    assert seen == []
    assert bus.has("e") is False


def test_unsubscribing_something_never_subscribed_is_harmless():
    bus = EventBus()
    bus.off("e", lambda ev: None)      # must not raise


def test_the_subscription_generation_moves_with_every_change():
    bus = EventBus()
    generations = [bus.gen]
    handler = bus.on("e", lambda ev: None)
    generations.append(bus.gen)
    bus.off("e", handler)
    generations.append(bus.gen)
    bus.clear()
    generations.append(bus.gen)
    assert len(set(generations)) == len(generations), (
        "the generation counter repeated: %s - the tick gate would miss a "
        "subscription change" % (generations,))


def test_an_events_data_is_reachable_three_ways():
    event = Event("e", {"amount": 5})
    assert event.get("amount") == 5
    assert event["amount"] == 5
    assert event.amount == 5
    assert event.get("missing", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Host binding
# ---------------------------------------------------------------------------

def test_binding_a_host_calls_connect_and_gives_the_plugin_its_host(
        plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")

    manager.bind_host(fake_logic(), kind="engine")

    assert ("connect", None) in plugin.calls
    assert manager.host_for(plugin) is plugin.host, (
        "the plugin was handed a different host object than the manager kept")


def test_a_plugin_whose_connect_raises_does_not_stop_the_others(
        plugin_manager, fake_logic):
    manager = plugin_manager("raises_on_connect", "well_behaved")
    good = manager.find_plugin("well_behaved")

    manager.bind_host(fake_logic())

    assert ("connect", None) in good.calls, (
        "a plugin that raised in connect() stopped the plugin after it from "
        "being connected")


def test_a_half_connected_plugin_keeps_the_subscription_it_managed(
        plugin_manager, fake_logic):
    """Documents what "fails safe" means here: safe, not clean.

    ``connect`` raised *after* subscribing, so the subscription is live and the
    plugin will keep receiving that event.  The host survives - which is the
    contract - but the plugin is not rolled back, and a test that asserted
    otherwise would be asserting a behaviour Fio does not have.
    """
    manager = plugin_manager("raises_on_connect")
    plugin = manager.find_plugin("raises_on_connect")
    manager.bind_host(fake_logic())

    manager.emit("test.ping", value=1)

    assert plugin.events_seen == [{"value": 1}], (
        "the subscription made before connect() raised is expected to remain "
        "live; it saw %s" % (plugin.events_seen,))


def test_rebinding_to_a_new_session_does_not_stack_subscriptions(
        plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")

    manager.bind_host(fake_logic())
    manager.bind_host(fake_logic())        # a second play session
    manager.emit("test.ping", value=1)

    assert plugin.events_seen == [{"value": 1}], (
        "the event was delivered %d times after two binds; each play session "
        "must not add another copy of every subscription"
        % len(plugin.events_seen))


def test_a_service_published_by_a_plugin_is_readable_by_another(
        plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    manager.bind_host(fake_logic())
    assert manager.services.get("well_behaved_service") is plugin


def test_the_host_exposes_the_bound_session(plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    logic = fake_logic(things=["a", "b"])
    manager.bind_host(logic, kind="engine")
    assert plugin.host.logic is logic
    assert plugin.host.scene == ["a", "b"]


def test_wants_tick_is_false_when_nothing_needs_a_frame(plugin_manager):
    manager = plugin_manager("factory_plugin")
    assert manager.wants_tick() is False, (
        "a plugin that overrides no per-frame hook should cost the engine "
        "nothing per frame")


def test_wants_tick_becomes_true_when_a_ticking_plugin_is_enabled(plugin_manager):
    manager = plugin_manager("well_behaved")
    assert manager.wants_tick() is True
    manager.set_enabled(manager.find_plugin("well_behaved"), False)
    assert manager.wants_tick() is False, (
        "a disabled plugin must not keep the per-frame dispatch alive")


# ---------------------------------------------------------------------------
# Lifecycle dispatch
# ---------------------------------------------------------------------------

def test_the_play_lifecycle_reaches_an_enabled_plugin(plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    logic = fake_logic()

    manager.dispatch_play_start(logic)
    manager.dispatch_play_stop(logic)

    hooks = [call[0] for call in plugin.calls]
    assert hooks.count("on_play_start") == 1, "on_play_start: %s" % (hooks,)
    assert hooks.count("on_play_stop") == 1, "on_play_stop: %s" % (hooks,)
    assert hooks.index("on_play_start") < hooks.index("on_play_stop")


def test_a_disabled_plugin_gets_no_lifecycle_callbacks(plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    manager.set_enabled(plugin, False)
    plugin.calls.clear()

    manager.dispatch_play_start(fake_logic())
    manager.dispatch_play_stop(fake_logic())

    assert [c for c in plugin.calls if c[0].startswith("on_play")] == [], (
        "a disabled plugin received %s" % (plugin.calls,))


def test_a_plugin_that_raises_on_play_start_does_not_stop_the_session(
        plugin_manager, fake_logic):
    manager = plugin_manager("raises_on_tick", "well_behaved")
    bomb = manager.find_plugin("raises_on_tick")
    good = manager.find_plugin("well_behaved")

    manager.dispatch_play_start(fake_logic())

    assert bomb.attempts["start"] == 1, "the raising plugin was not called"
    assert ("on_play_start", None) in good.calls, (
        "a plugin that raised in on_play_start stopped the plugins after it "
        "from starting")


def test_a_plugin_that_raises_every_tick_does_not_stop_the_others(
        plugin_manager, fake_logic):
    from plugins.api import TickContext
    manager = plugin_manager("raises_on_tick", "well_behaved")
    bomb = manager.find_plugin("raises_on_tick")
    good = manager.find_plugin("well_behaved")
    logic = fake_logic()

    for _ in range(5):
        manager.dispatch_tick(logic, TickContext(logic, keys=set()))

    assert bomb.attempts["tick"] == 5, (
        "the raising plugin was called %d times, expected 5 - the host stopped "
        "dispatching to it" % bomb.attempts["tick"])
    assert good.tick_count == 5, (
        "the good plugin ticked %d times; a neighbour's exception cost it "
        "frames" % good.tick_count)


def test_the_manager_survives_a_plugin_raising_on_play_stop(
        plugin_manager, fake_logic):
    manager = plugin_manager("raises_on_tick", "well_behaved")
    manager.dispatch_play_stop(fake_logic())      # must not raise
    assert manager.find_plugin("raises_on_tick").attempts["stop"] == 1


# ---------------------------------------------------------------------------
# Runtime attach
# ---------------------------------------------------------------------------

def test_attaching_the_runtime_calls_every_plugin(plugin_manager, fake_logic):
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    manager.attach_runtime(fake_logic())
    assert ("register_runtime", None) in plugin.calls


def test_a_disabled_plugin_still_attaches_so_it_works_when_enabled_later(
        plugin_manager, fake_logic):
    """A level can auto-enable a plugin after the logic thread was built."""
    manager = plugin_manager("well_behaved")
    plugin = manager.find_plugin("well_behaved")
    manager.set_enabled(plugin, False)
    plugin.calls.clear()

    manager.attach_runtime(fake_logic())

    assert ("register_runtime", None) in plugin.calls, (
        "a disabled plugin was not attached; enabling it mid-session would "
        "leave its inputs dead")


def test_duplicate_input_handler_registration_leaves_the_last_one_winning(
        plugin_manager, fake_logic):
    pytest.importorskip("PyQt5", reason="the I/O manager lives in the editor package")
    from editor.io_system import IOManager
    from tests.helpers.worlds import box_brush

    manager = plugin_manager("duplicate_handlers")
    plugin = manager.find_plugin("duplicate_handlers")
    logic = fake_logic()
    logic.io_manager = IOManager()
    target = box_brush("target")
    logic.io_manager.set_entity_finder(lambda name: target)

    manager.attach_runtime(logic)
    logic.io_manager._execute_input("target", "Twice", "", "test")

    assert (plugin.first_calls, plugin.second_calls) == (0, 1), (
        "registering the same (type, input) twice should leave exactly one "
        "handler installed - the last - but first ran %d times and second %d"
        % (plugin.first_calls, plugin.second_calls))


def test_subscribing_the_same_handler_twice_delivers_twice(
        plugin_manager, fake_logic):
    """Documents that the bus does not de-duplicate subscriptions."""
    manager = plugin_manager("duplicate_handlers")
    plugin = manager.find_plugin("duplicate_handlers")
    manager.bind_host(fake_logic())

    manager.emit("test.dup")

    assert plugin.event_calls == 2, (
        "the event bus keeps both subscriptions, so a double subscribe means "
        "double delivery; the handler ran %d times" % plugin.event_calls)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_two_managers_do_not_share_loaded_plugins(plugin_manager):
    first = plugin_manager("well_behaved")
    second = plugin_manager("factory_plugin")
    assert [p.name for p in first.plugins] == ["well_behaved"]
    assert [p.name for p in second.plugins] == ["factory_plugin"]


def test_a_plugins_global_store_is_namespaced(plugin_manager):
    manager = plugin_manager("well_behaved")
    api = EditorAPI(manager, manager.find_plugin("well_behaved"))
    api.global_set("score", 7, store="plugin_a")
    assert api.global_get("score", store="plugin_a") == "7", (
        "values are stored as strings, matching the map key/value store a "
        "plugin can share with; got %r"
        % (api.global_get("score", store="plugin_a"),))
    assert api.global_get("score", store="plugin_b") is None, (
        "a value written to one store was visible in another")


def test_the_global_store_returns_the_default_for_a_missing_key(plugin_manager):
    manager = plugin_manager("well_behaved")
    api = EditorAPI(manager, manager.find_plugin("well_behaved"))
    assert api.global_get("never_set", "fallback") == "fallback"


def test_an_unloaded_plugin_never_appears_in_the_dispatch_lists(plugin_manager,
                                                                fake_logic):
    manager = plugin_manager("raises_on_register", "well_behaved")
    manager.dispatch_play_start(fake_logic())
    # Nothing to assert about the refused plugin except that it was never run;
    # the good one running is the observable half.
    assert ("on_play_start", None) in manager.find_plugin("well_behaved").calls


# ---------------------------------------------------------------------------
# The headless boundary
# ---------------------------------------------------------------------------

def test_the_plugin_api_does_not_pull_in_qt_or_opengl():
    """A plugin must be loadable in the player, where neither exists.

    Checked in a clean subprocess, because by the time this suite runs, some
    other test has certainly imported PyQt5 already.
    """
    import subprocess
    import sys

    from tests.helpers.paths import REPO_ROOT

    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import plugins.api, plugins.host, plugins.manager, plugins.entitybase\n"
        "heavy = [m for m in ('PyQt5', 'OpenGL', 'glm', 'pygame')\n"
        "         if m in sys.modules]\n"
        "assert not heavy, 'importing the plugin API pulled in %%s' %% (heavy,)\n"
        "print('OK')\n" % REPO_ROOT)
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, (
        "importing the plugin API is not dependency-light:\n%s%s"
        % (result.stdout[-500:], result.stderr[-500:]))
