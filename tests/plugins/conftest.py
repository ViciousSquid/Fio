"""Fixtures for the plugin contract suite.

The plugin manager discovers plugins by scanning its own package directory and
importing ``plugins.<name>``.  The suite's fixture plugins deliberately do not
live there — nothing a test writes should be discoverable by a real Fio launch
or packaged into an export — so the discovery fixture redirects both halves of
that lookup at ``tests/plugins/fixtures`` for the duration of a test.

The redirection is the only thing faked.  Everything after it is the real
:class:`plugins.manager.PluginManager` running its real load, validation,
attach, bind and dispatch path, on a *fresh* instance rather than the
process-wide singleton, so one test cannot leave a plugin behind for the next.
"""

import importlib
import os
import pkgutil

import pytest

from plugins.manager import PluginManager

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
FIXTURES_PACKAGE = "tests.plugins.fixtures"

#: Every fixture plugin package, so a test can name the ones it wants.
ALL_FIXTURES = sorted(
    entry.name for entry in pkgutil.iter_modules([FIXTURES_DIR]) if entry.ispkg)


@pytest.fixture
def plugin_manager(monkeypatch):
    """A fresh manager whose discovery is pointed at the fixture plugins.

    Call the returned factory with the package names to expose::

        manager = plugin_manager("well_behaved", "raises_on_tick")

    Names that do not exist are passed through unchanged, so a test can check
    what happens when discovery names a package that cannot be imported.
    """
    import plugins.manager as manager_module

    def _build(*names):
        wanted = list(names) if names else list(ALL_FIXTURES)
        manager = PluginManager()

        def _fake_iter_modules(paths):
            return [pkgutil.ModuleInfo(None, name, True) for name in wanted]

        real_import = importlib.import_module

        def _fake_import(module_name, *args, **kwargs):
            if module_name.startswith("plugins."):
                suffix = module_name[len("plugins."):]
                if suffix in wanted:
                    return real_import("%s.%s" % (FIXTURES_PACKAGE, suffix))
            return real_import(module_name, *args, **kwargs)

        monkeypatch.setattr(manager_module.pkgutil, "iter_modules",
                            _fake_iter_modules)
        monkeypatch.setattr(manager_module.importlib, "import_module",
                            _fake_import)
        manager.discover_and_load()
        return manager

    return _build


@pytest.fixture(autouse=True)
def _reset_fixture_plugin_state():
    """Fixture plugins are module-level singletons; clear their logs each test."""
    yield
    for name in ALL_FIXTURES:
        module = __import__("%s.%s" % (FIXTURES_PACKAGE, name),
                            fromlist=["PLUGIN"])
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue
        for attribute, empty in (("calls", []), ("events_seen", []),
                                 ("tick_count", 0), ("ticks", 0),
                                 ("first_calls", 0), ("second_calls", 0),
                                 ("event_calls", 0)):
            if hasattr(plugin, attribute):
                setattr(plugin, attribute, list(empty) if isinstance(empty, list)
                        else empty)
        if hasattr(plugin, "attempts"):
            plugin.attempts = {"start": 0, "tick": 0, "stop": 0}
        # ``set_enabled`` writes an instance attribute; dropping it restores the
        # class default the plugin ships with, so "disabled by default" really
        # is the state the next test starts from.
        plugin.__dict__.pop("enabled", None)


class FakeLogic:
    """The slice of the logic thread the plugin runtime surface reaches for."""

    def __init__(self, things=(), brushes=()):
        self.things = list(things)
        self.brushes = list(brushes)
        self.play_mode = False
        self.player = None
        self.io_manager = None
        self.game_state = None
        self.terrain = None
        self.monster_ai = None


@pytest.fixture
def fake_logic():
    return FakeLogic
