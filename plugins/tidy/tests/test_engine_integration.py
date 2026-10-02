"""
Headless tests for the native engine<->plugin integration contract, exercised
through the plugin manager with a fake I/O manager and logic object (no editor,
no PyQt, no real LogicThread).

Covers:
* input handlers attach for *all* loaded plugins, even one disabled at attach
  time, and are gated by live enabled state (the regression the disabled-by-
  default change would otherwise cause);
* the cached, early-out per-tick dispatch (manager.tick / _active_for).

Run from the repo root:  python plugins/tidy/tests/test_engine_integration.py
"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _tidy(mgr):
    for p in mgr.plugins:
        if p.name == "tidy":
            return p
    raise AssertionError("tidy plugin not loaded")


class _FakeIO:
    """Minimal IOManager stand-in: stores handlers and can fire them."""

    def __init__(self):
        self.handlers = {}

    def register_input_handler(self, etype, iname, handler):
        self.handlers[(etype.lower(), iname.lower())] = handler

    def fire_input(self, etype, iname, entity, param=None, logic=None):
        h = self.handlers.get((etype.lower(), iname.lower()))
        return h(entity, param, logic) if h else None

    def fire_output(self, *a, **k):
        pass


class _FakeLogic:
    def __init__(self, io):
        self.io_manager = io
        self.things = []
        self.current_hud_message = ""


class _Ent:
    def __init__(self):
        self.properties = {"disabled": True}


def test_handlers_attach_regardless_of_enabled():
    print("[1] input handlers attach for a plugin disabled at attach time")
    from plugins.manager import load_plugins, get_manager
    load_plugins()
    mgr = get_manager()
    tidy = _tidy(mgr)

    # Disabled BEFORE the logic thread attaches (mirrors disabled-by-default).
    mgr.set_enabled(tidy, False)
    io = _FakeIO()
    logic = _FakeLogic(io)
    mgr.attach_runtime(logic)
    _check(("tidygoal", "enable") in io.handlers,
           "handler registered even though the plugin was disabled at attach")

    # While disabled, the gated handler is inert.
    ent = _Ent()
    io.fire_input("tidygoal", "enable", ent, None, logic)
    _check(ent.properties["disabled"] is True,
           "disabled plugin's input handler is a no-op")

    # Enabling later (as auto-enable would) makes the same handler live — no
    # re-attach needed.
    mgr.set_enabled(tidy, True)
    io.fire_input("tidygoal", "enable", ent, None, logic)
    _check(ent.properties["disabled"] is False,
           "handler runs once the plugin is enabled, without re-attaching")


def test_tick_cache_and_early_out():
    print("[2] cached, early-out per-tick dispatch")
    from plugins.manager import get_manager
    mgr = get_manager()
    tidy = _tidy(mgr)

    mgr.set_enabled(tidy, False)
    _check(mgr._active_for("on_tick") == [],
           "no active tickers while tidy is disabled")

    gen = mgr._enabled_generation
    mgr.set_enabled(tidy, True)
    _check(mgr._enabled_generation != gen,
           "enabling bumps the generation so caches invalidate")
    _check(tidy in mgr._active_for("on_tick"),
           "tidy (which overrides on_tick) is an active ticker when enabled")

    # A no-op set_enabled (same state) must NOT bump the generation.
    gen2 = mgr._enabled_generation
    mgr.set_enabled(tidy, True)
    _check(mgr._enabled_generation == gen2,
           "re-setting the same enabled state doesn't churn the cache")

    # manager.tick is a safe no-op when nothing ticks, and doesn't raise when it
    # does (tidy.on_tick early-returns with no session on the logic object).
    io = _FakeIO()
    logic = _FakeLogic(io)
    mgr.set_enabled(tidy, False)
    mgr.tick(logic, use_pressed=True, delta=0.016)   # early-out, no crash
    mgr.set_enabled(tidy, True)
    mgr.tick(logic, use_pressed=True, delta=0.016)    # dispatches, no crash
    _check(True, "manager.tick handles both empty and active cases")


def test_overrides_detection():
    print("[3] only hook-overriding plugins are dispatched")
    from plugins.manager import PluginManager
    from plugins.api import FioPlugin

    class _Idle(FioPlugin):
        name = "idle-test"

    class _Ticker(FioPlugin):
        name = "ticker-test"

        def on_tick(self, logic, ctx):
            pass

    _check(PluginManager._overrides(_Ticker(), "on_tick") is True,
           "a plugin that implements on_tick is detected as overriding")
    _check(PluginManager._overrides(_Idle(), "on_tick") is False,
           "a plugin that doesn't implement on_tick is skipped")


def main():
    test_handlers_attach_regardless_of_enabled()
    test_tick_cache_and_early_out()
    test_overrides_detection()
    print("\nALL ENGINE-INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
