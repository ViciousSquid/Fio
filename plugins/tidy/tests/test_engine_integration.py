"""
Tests for the native engine<->plugin integration contract, exercised through
the real PluginManager, LogicThread and IOManager. Only the process-wide plugin
singleton is disabled for these focused tests so a fresh manager owns the
plugin lifecycle under test.

Covers:
* input handlers attach for *all* loaded plugins, even one disabled at attach
  time, and are gated by live enabled state (the regression the disabled-by-
  default change would otherwise cause);
* the cached, early-out per-tick dispatch (manager.tick / _active_for).

Run from the repo root:  python plugins/tidy/tests/test_engine_integration.py
"""

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# Only when there is no display: the offscreen plugin cannot create an
# OpenGL context, and forcing it here would disable the visual tier for
# the whole session when the suite is run under Xvfb.
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5", reason="the engine integration test constructs a real LogicThread")
pytestmark = pytest.mark.qt


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _tidy(mgr):
    for p in mgr.plugins:
        if p.name == "tidy":
            return p
    raise AssertionError("tidy plugin not loaded")


def _real_logic(monkeypatch):
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    # Keep the process-wide engine plugin singleton out of this focused manager
    # test. The manager under test is a fresh real PluginManager, while the
    # LogicThread/IOManager remain production objects.
    monkeypatch.setenv("FIO_NO_PLUGINS", "1")
    return LogicThread(ThreadedGameState(), EditorState())


def _fresh_manager():
    from plugins.manager import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    return manager


def _tidy(mgr):
    for plugin in mgr.plugins:
        if plugin.name == "tidy":
            return plugin
    raise AssertionError("tidy plugin not loaded")


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def test_handlers_attach_regardless_of_enabled(monkeypatch):
    print("[1] input handlers attach for a plugin disabled at attach time")
    from plugins.tidy.entities import TidyGoal

    logic = _real_logic(monkeypatch)
    try:
        mgr = _fresh_manager()
        tidy = _tidy(mgr)

        # Disabled BEFORE the real LogicThread receives the plugin runtime.
        mgr.set_enabled(tidy, False)
        mgr.attach_runtime(logic)
        _check(("tidygoal", "enable") in logic.io_manager._input_handlers,
               "handler registered even though the plugin was disabled at attach")

        # While disabled, the real IOManager dispatch is inert.
        ent = TidyGoal(
            pos=[0.0, 0.0, 0.0],
            properties={"name": "goal", "disabled": True},
        )
        logic.editor_state.things.append(ent)
        logic.world_runtime.build_entity_caches()
        logic.io_manager._execute_input(
            "goal", "Enable", "", "test", target_id=ent.properties["id"]
        )
        _check(ent.properties["disabled"] is True,
               "disabled plugin's input handler is a no-op")

        # Enabling later makes the same real handler live without re-attaching.
        mgr.set_enabled(tidy, True)
        logic.io_manager._execute_input(
            "goal", "Enable", "", "test", target_id=ent.properties["id"]
        )
        _check(ent.properties["disabled"] is False,
               "handler runs once the plugin is enabled, without re-attaching")
    finally:
        logic.stop()


def test_tick_cache_and_early_out(monkeypatch):
    print("[2] cached, early-out per-tick dispatch")
    logic = _real_logic(monkeypatch)
    try:
        mgr = _fresh_manager()
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

        gen2 = mgr._enabled_generation
        mgr.set_enabled(tidy, True)
        _check(mgr._enabled_generation == gen2,
               "re-setting the same enabled state doesn't churn the cache")

        mgr.set_enabled(tidy, False)
        mgr.tick(logic, use_pressed=True, delta=0.016)
        mgr.set_enabled(tidy, True)
        mgr.tick(logic, use_pressed=True, delta=0.016)
        _check(True, "manager.tick handles both empty and active cases")
    finally:
        logic.stop()


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


