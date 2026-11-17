"""Release-hardening regressions for the plugin host.

Each section is one defect that shipped: a disabled plugin still shadowing a
core input handler, ``host.off`` forgetting all but the last event a handler was
subscribed to, the play lifecycle going unbalanced when a plugin is toggled
mid-Play, a hook that raises every tick logging a traceback every tick for
ever, ``PropertySpec.coerce`` raising, and ``FioPlugin.requires`` being one list
shared by every plugin.

All headless: plugins are plain ``FioPlugin`` subclasses added to a fresh
``PluginManager``, and the I/O side is a bare ``IOManager`` over brush dicts.
"""

import pytest

from editor.io_system import IOManager
from plugins.api import FioPlugin, PropertySpec, RuntimeAPI, TickContext
from plugins.host import PluginHost
from plugins.manager import PluginManager
from tests.helpers.worlds import box_brush


class Logic:
    """The slice of the logic thread the plugin runtime reaches for."""

    def __init__(self):
        self.io_manager = None
        self.editor_state = type("EditorState", (), {"things": [], "brushes": []})()
        self.session_runtime = type("SessionRuntime", (), {"play_mode": False})()
        self.player = None
        self.game_state = None
        self.monster_ai = None


@pytest.fixture
def manager(monkeypatch):
    value = PluginManager()
    value.messages = []
    monkeypatch.setattr(value, "_log", value.messages.append)
    return value


def add(manager, plugin):
    manager.plugins.append(plugin)
    manager._enabled_generation += 1
    return plugin


# ---------------------------------------------------------------------------
# A disabled plugin does not shadow the handler it replaced
# ---------------------------------------------------------------------------

class Override(FioPlugin):
    def __init__(self, name, input_name="Open", entity_type="door"):
        self.name = name
        self.seen = []
        self._key = (entity_type, input_name)

    def register_runtime(self, api):
        api.register_input_handler(self._key[0], self._key[1], self._handle)

    def _handle(self, entity, parameter, logic):
        self.seen.append(parameter)


@pytest.fixture
def io_logic():
    logic = Logic()
    logic.io_manager = IOManager()
    logic.io_manager.set_logic_thread(logic)
    logic.target = box_brush("door", is_door=True)
    logic.io_manager.set_entity_finder(lambda name: logic.target)
    logic.core_calls = []
    logic.io_manager.register_input_handler(
        "door", "Open", lambda e, p, l: logic.core_calls.append(p))
    return logic


def deliver(logic, input_name="Open", parameter="x"):
    logic.io_manager._execute_input("door", input_name, parameter, "test")


def test_a_disabled_plugin_leaves_the_core_handler_working(manager, io_logic):
    plugin = add(manager, Override("over"))
    plugin.enabled = False
    manager.attach_runtime(io_logic)

    deliver(io_logic)

    assert io_logic.core_calls == ["x"], (
        "a DISABLED plugin that registered door.Open swallowed the input: the "
        "core handler ran %d times, expected 1" % len(io_logic.core_calls))
    assert plugin.seen == [], "the disabled plugin's handler must not run"


def test_an_enabled_plugin_still_overrides_the_core_handler(manager, io_logic):
    plugin = add(manager, Override("over"))
    manager.attach_runtime(io_logic)

    deliver(io_logic)

    assert (plugin.seen, io_logic.core_calls) == (["x"], []), (
        "an enabled plugin's handler replaces the core one (documented); "
        "plugin saw %s, core saw %s" % (plugin.seen, io_logic.core_calls))


def test_the_override_follows_the_live_enabled_flag(manager, io_logic):
    plugin = add(manager, Override("over"))
    manager.attach_runtime(io_logic)

    deliver(io_logic, parameter="1")
    manager.set_enabled(plugin, False)
    deliver(io_logic, parameter="2")
    manager.set_enabled(plugin, True)
    deliver(io_logic, parameter="3")

    assert (plugin.seen, io_logic.core_calls) == (["1", "3"], ["2"]), (
        "the input should go to the plugin while it is on and back to the core "
        "handler while it is off; plugin saw %s, core saw %s"
        % (plugin.seen, io_logic.core_calls))


def test_a_disabled_plugin_does_not_shadow_a_generic_input(manager, io_logic):
    """``Disable`` has no registered core handler: it is a generic input."""
    plugin = add(manager, Override("over", input_name="Disable"))
    plugin.enabled = False
    manager.attach_runtime(io_logic)

    deliver(io_logic, input_name="Disable", parameter="")

    assert io_logic.target.get("disabled") is True, (
        "a disabled plugin that registered door.Disable stopped the generic "
        "Disable input from working")


def test_a_disabled_plugins_own_entity_type_stays_inert(manager, io_logic):
    """Documented: a disabled plugin's own inputs do nothing at all."""
    plugin = add(manager, Override("over", input_name="Disable"))
    plugin.enabled = False
    manager._entity_owner[manager._normalise_type("door")] = plugin
    manager.attach_runtime(io_logic)

    deliver(io_logic, input_name="Disable", parameter="")

    assert not io_logic.target.get("disabled"), (
        "an input on an entity type the disabled plugin owns must stay inert")


def test_two_plugins_on_one_input_chain_and_are_reported_once(manager, io_logic):
    first = add(manager, Override("first"))
    second = add(manager, Override("second"))
    manager.attach_runtime(io_logic)
    # A second logic thread (a new session) must not repeat the report.
    other = Logic()
    other.io_manager = IOManager()
    other.io_manager.register_input_handler("door", "Open", lambda e, p, l: None)
    manager.attach_runtime(other)

    collisions = [m for m in manager.messages if "both handle" in m]
    assert len(collisions) == 1, (
        "two plugins claiming door.Open should be logged exactly once, got %d: "
        "%s" % (len(collisions), collisions))
    assert "first" in collisions[0] and "second" in collisions[0], collisions[0]

    deliver(io_logic, parameter="a")
    manager.set_enabled(second, False)
    deliver(io_logic, parameter="b")
    manager.set_enabled(first, False)
    deliver(io_logic, parameter="c")

    assert (second.seen, first.seen, io_logic.core_calls) == (["a"], ["b"], ["c"]), (
        "the last-registered enabled plugin wins, then the one before it, then "
        "the core handler; second=%s first=%s core=%s"
        % (second.seen, first.seen, io_logic.core_calls))


def test_one_plugin_registering_twice_is_not_a_collision(manager, io_logic):
    class Twice(Override):
        def register_runtime(self, api):
            api.register_input_handler("door", "Open", lambda e, p, l: self.seen.append("old"))
            api.register_input_handler("door", "Open", self._handle)

    plugin = add(manager, Twice("twice"))
    manager.attach_runtime(io_logic)
    deliver(io_logic)
    manager.set_enabled(plugin, False)
    deliver(io_logic, parameter="y")

    assert plugin.seen == ["x"], "the last registration wins: %s" % plugin.seen
    assert io_logic.core_calls == ["y"], (
        "disabled, the input must fall through both of the plugin's own "
        "registrations to the core handler; core saw %s" % io_logic.core_calls)
    assert not [m for m in manager.messages if "both handle" in m]


# ---------------------------------------------------------------------------
# host.off
# ---------------------------------------------------------------------------

def test_off_unsubscribes_the_event_it_names_when_a_handler_has_several(manager):
    plugin = add(manager, FioPlugin())
    host = PluginHost(manager, Logic(), plugin)
    seen = []

    def handler(event):
        seen.append(event.name)

    host.on("a", handler)
    host.on("b", handler)
    host.off("a", handler)
    manager.emit("a")
    manager.emit("b")

    assert seen == ["b"], (
        "off('a', h) must stop 'a' and leave 'b'; the handler was called for %s"
        % (seen,))

    host.off("b", handler)
    manager.emit("b")
    assert seen == ["b"], "off('b', h) left 'b' subscribed"
    assert not manager.has_listeners("a") and not manager.has_listeners("b")


def test_off_removes_every_subscription_of_a_handler_to_that_event(manager):
    plugin = add(manager, FioPlugin())
    host = PluginHost(manager, Logic(), plugin)
    seen = []
    handler = seen.append
    host.on("a", handler)
    host.on("a", handler)
    host.off("a", handler)
    manager.emit("a")
    assert seen == [], (
        "a handler subscribed twice was still called %d time(s) after off()"
        % len(seen))


def test_off_for_a_handler_that_was_never_subscribed_is_a_no_op(manager):
    host = PluginHost(manager, Logic(), add(manager, FioPlugin()))
    host.off("a", lambda event: None)          # must not raise
    seen = []
    wrapper = host.on("a", seen.append)
    host.off("a", wrapper)                     # the value on() returned works too
    manager.emit("a")
    assert seen == []


# ---------------------------------------------------------------------------
# Toggling a plugin in the middle of a play session
# ---------------------------------------------------------------------------

class Lifecycle(FioPlugin):
    def __init__(self, name="life"):
        self.name = name
        self.calls = []

    def on_play_start(self, logic):
        self.calls.append("start")

    def on_play_stop(self, logic):
        self.calls.append("stop")

    def on_tick(self, logic, ctx):
        self.calls.append("tick")

    def on_enabled_changed(self, enabled):
        self.calls.append("enabled=%s" % enabled)


def test_disabling_mid_play_runs_on_play_stop_once(manager):
    plugin = add(manager, Lifecycle())
    logic = Logic()
    manager.dispatch_play_start(logic)
    manager.tick(logic)

    manager.set_enabled(plugin, False)
    manager.tick(logic)
    manager.dispatch_play_stop(logic)

    assert plugin.calls == ["start", "tick", "stop", "enabled=False"], (
        "a plugin disabled during Play is owed exactly one on_play_stop (it "
        "restores what it mutated there) and no further ticks; got %s"
        % (plugin.calls,))


def test_enabling_mid_play_runs_on_play_start_before_the_first_tick(manager):
    plugin = add(manager, Lifecycle())
    plugin.enabled = False
    logic = Logic()
    manager.dispatch_play_start(logic)
    manager.tick(logic)

    manager.set_enabled(plugin, True)
    manager.tick(logic)
    manager.dispatch_play_stop(logic)

    assert plugin.calls == ["enabled=True", "start", "tick", "stop"], (
        "a plugin enabled during Play must get on_play_start before it is "
        "ticked, and one on_play_stop at the end; got %s" % (plugin.calls,))


def test_toggling_outside_play_dispatches_no_play_hooks(manager):
    plugin = add(manager, Lifecycle())
    manager.set_enabled(plugin, False)
    manager.set_enabled(plugin, True)
    logic = Logic()
    manager.dispatch_play_start(logic)
    manager.dispatch_play_stop(logic)
    manager.set_enabled(plugin, False)
    manager.set_enabled(plugin, True)

    assert plugin.calls == ["enabled=False", "enabled=True", "start", "stop",
                            "enabled=False", "enabled=True"], (
        "with no session live, enable/disable must not call the play hooks; "
        "got %s" % (plugin.calls,))


def test_a_repeated_toggle_mid_play_keeps_start_and_stop_paired(manager):
    plugin = add(manager, Lifecycle())
    logic = Logic()
    manager.dispatch_play_start(logic)
    for _ in range(3):
        manager.set_enabled(plugin, False)
        manager.set_enabled(plugin, False)     # a no-op repeat
        manager.set_enabled(plugin, True)
    manager.dispatch_play_stop(logic)

    hooks = [c for c in plugin.calls if c in ("start", "stop")]
    assert hooks == ["start", "stop"] * 4, (
        "every on_play_start must be matched by one on_play_stop; got %s"
        % (hooks,))


def test_a_mid_play_toggle_is_serialised_with_the_tick_lock(manager):
    events = []

    class Lock:
        def __enter__(self):
            events.append("lock")

        def __exit__(self, *exc):
            events.append("unlock")

    class P(Lifecycle):
        def on_play_stop(self, logic):
            events.append("stop")

    plugin = add(manager, P())
    logic = Logic()
    logic._tick_lock = Lock()
    manager.dispatch_play_start(logic)
    manager.set_enabled(plugin, False)

    assert events == ["lock", "stop", "unlock"], (
        "on_play_stop for a mid-Play disable must run under the logic tick "
        "lock so it cannot race the plugin's own on_tick; got %s" % (events,))


def test_a_raising_on_play_stop_still_disables_the_plugin(manager):
    class P(Lifecycle):
        def on_play_stop(self, logic):
            raise RuntimeError("stop failed on purpose")

    plugin = add(manager, P())
    manager.dispatch_play_start(Logic())
    manager.set_enabled(plugin, False)         # must not raise
    assert plugin.enabled is False
    assert any("on_play_stop() failed" in m for m in manager.messages)


# ---------------------------------------------------------------------------
# A hook that raises every tick
# ---------------------------------------------------------------------------

class Bomb(FioPlugin):
    name = "bomb"

    def __init__(self, every=1):
        self.ticks = 0
        self.every = every
        self.stopped = 0

    def on_tick(self, logic, ctx):
        self.ticks += 1
        if self.ticks % self.every == 0:
            raise RuntimeError("tick failed on purpose")

    def on_play_stop(self, logic):
        self.stopped += 1


@pytest.mark.parametrize("entry", ["tick", "dispatch_tick"])
def test_a_plugin_failing_every_tick_is_disabled_after_the_limit(manager, entry):
    bomb = add(manager, Bomb())
    good = add(manager, Lifecycle("good"))
    logic = Logic()
    manager.dispatch_play_start(logic)

    for _ in range(100):
        if entry == "tick":
            manager.tick(logic)
        else:
            manager.dispatch_tick(logic, TickContext(logic, keys=set()))

    limit = PluginManager.MAX_HOOK_FAILURES
    assert bomb.ticks == limit, (
        "a hook that raises every tick should be called %d times and then "
        "left alone; it was called %d times" % (limit, bomb.ticks))
    assert bomb.enabled is False, "the failing plugin was not disabled"
    assert bomb.stopped == 1, (
        "disabling goes through set_enabled, so a mid-Play disable still runs "
        "on_play_stop exactly once (ran %d times)" % bomb.stopped)
    assert good.calls.count("tick") == 100, (
        "the healthy plugin lost ticks to its neighbour's failures")
    disabled = [m for m in manager.messages if "disabled" in m and "bomb" in m]
    assert len(disabled) == 1 and "on_tick" in disabled[0] \
        and "tick failed on purpose" in disabled[0], (
        "expected one clear 'disabled' message naming the hook and the "
        "error, got %s" % (disabled,))
    tracebacks = [m for m in manager.messages if "Traceback" in m]
    assert len(tracebacks) <= PluginManager.HOOK_TRACEBACK_LIMIT, (
        "%d tracebacks were logged for %d failures; logging must be "
        "rate-limited" % (len(tracebacks), limit))
    assert len(manager.messages) < limit, (
        "%d messages for %d failures" % (len(manager.messages), limit))


def test_an_intermittent_failure_does_not_disable_but_is_rate_limited(manager):
    bomb = add(manager, Bomb(every=2))          # fails every other tick
    logic = Logic()
    manager.dispatch_play_start(logic)
    for _ in range(2000):
        manager.tick(logic)

    assert bomb.enabled is True and bomb.ticks == 2000, (
        "a hook that recovers between failures is not 'failing every tick' "
        "and must stay enabled")
    assert len(manager.messages) <= 16, (
        "1000 intermittent failures produced %d log messages; they must thin "
        "out" % len(manager.messages))
    assert sum("Traceback" in m for m in manager.messages) \
        == PluginManager.HOOK_TRACEBACK_LIMIT


def test_re_enabling_a_disabled_plugin_gives_it_a_fresh_count(manager):
    bomb = add(manager, Bomb())
    logic = Logic()
    for _ in range(50):
        manager.tick(logic)
    assert bomb.enabled is False
    del manager.messages[:]

    manager.set_enabled(bomb, True)
    for _ in range(PluginManager.MAX_HOOK_FAILURES - 1):
        manager.tick(logic)

    assert bomb.enabled is True, (
        "a re-enabled plugin starts a new failure streak, not the old one")
    assert any("Traceback" in m for m in manager.messages), (
        "a re-enabled plugin's first failure should be logged in full again")


# ---------------------------------------------------------------------------
# PropertySpec.coerce never raises; FioPlugin.requires is not shared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [float("inf"), float("-inf"), "1e999",
                                   "-1e999", "inf", float("nan"), "nan",
                                   None, "abc", [], 10 ** 400])
def test_int_coercion_never_raises(value):
    spec = PropertySpec("count", "int", default=7, min=0, max=100)
    try:
        result = spec.coerce(value)
    except Exception as exc:                                  # noqa: BLE001
        pytest.fail("PropertySpec.coerce(%r) raised %s; it is documented "
                    "never to raise" % (value, type(exc).__name__))
    assert isinstance(result, int) and 0 <= result <= 100, (
        "coerce(%r) returned %r, expected an int in range" % (value, result))


def test_an_uncastable_int_falls_back_to_the_default_or_zero():
    assert PropertySpec("n", "int", default=7).coerce(float("inf")) == 7
    assert PropertySpec("n", "int").coerce("1e999") == 0
    assert PropertySpec("n", "int", default=7).coerce("12.9") == 12, (
        "ordinary values must coerce as before")


def test_requires_is_not_shared_between_plugins():
    class A(FioPlugin):
        name = "a"

    class B(FioPlugin):
        name = "b"

    a, b = A(), B()
    a.requires.append("something")

    assert a.requires == ["something"], (
        "a plugin's own requires list must keep what was appended to it")
    assert b.requires == [], (
        "appending to one plugin's requires leaked into another plugin: %r"
        % (b.requires,))
    assert FioPlugin.requires == [] and A.requires == [], (
        "the class default was mutated: FioPlugin.requires=%r"
        % (FioPlugin.requires,))
    assert isinstance(FioPlugin().requires, list)


def test_a_declared_requires_still_overrides_the_default():
    class C(FioPlugin):
        requires = ["well_behaved"]

    plugin = C()
    assert plugin.requires == ["well_behaved"]
    plugin.requires = ["x"]
    assert plugin.requires == ["x"] and C.requires == ["well_behaved"]
