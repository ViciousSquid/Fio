"""Repo-wide pytest configuration.

This file exists so that *every* Fio test — the unified suite under ``tests/``
and the tests the plugin and player packages ship with — runs under the same
rules:

* Qt is forced onto the offscreen platform plugin, so nothing needs a display
  server;
* the RNGs Fio's procedural code reaches for are reseeded before each test, so
  a failure reproduces;
* the process-wide singletons Fio uses (the plugin manager, the I/O revision
  counter, the ``Thing`` name counters) are reset between tests, so a test
  cannot be made to pass or fail by whatever ran before it;
* the ``gl`` tier skips itself with a readable reason when no OpenGL context
  can be created, rather than erroring somewhere deep in PyOpenGL.

See ``tests/README.md`` for how the tiers are meant to be run.
"""

import os
import random
import sys

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Must be set before anything imports PyQt5: a QApplication built against the
# real platform plugin aborts the process when there is no display.  A display
# *is* honoured when one is present, because the offscreen plugin cannot create
# an OpenGL context — the visual tier needs the real one (under Xvfb in CI).
if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Qt writes its runtime files here; without it every Qt test prints a warning.
os.environ.setdefault("XDG_RUNTIME_DIR", os.path.join("/tmp", "fio-test-runtime"))
try:
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], mode=0o700, exist_ok=True)
except OSError:  # pragma: no cover - read-only /tmp is not a test failure
    pass

#: Seed used for every test that does not ask for another.  Property/fuzz tests
#: derive their own seeds from this so a whole run is reproducible from one
#: number.
DEFAULT_SEED = 20240914


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--run-benchmarks", action="store_true", default=False,
        help="run the renderer/engine benchmarks (they report timings and are "
             "excluded from the normal suite)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-benchmarks"):
        return
    skip = pytest.mark.skip(reason="benchmark tier: pass --run-benchmarks to run it")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _deterministic_rngs():
    """Reseed the global RNGs before every test.

    Fio's procedural generators, the Tidy plugin's placement and several engine
    paths use :mod:`random`; NumPy's legacy global RNG shows up in the terrain
    code.  Seeding them here means a failure in a generated test reproduces from
    the seed printed in its id rather than from "whatever ran before it".
    """
    random.seed(DEFAULT_SEED)
    try:
        import numpy as np
        np.random.seed(DEFAULT_SEED & 0xFFFFFFFF)
    except Exception:  # pragma: no cover - numpy is a hard dep in practice
        pass
    yield


@pytest.fixture(scope="session", autouse=True)
def _plugins_loaded_before_isolation():
    """Load the plugins once, before any per-test registry snapshot is taken.

    ``_isolate_process_singletons`` restores ``IO_REGISTRY`` to whatever it held
    at the *start of each test*, so a test that registers a fake entity type
    cannot leak it into the next one. But plugin registration is a
    once-per-process event: ``PluginManager.discover_and_load`` early-outs on
    ``self._loaded``. So if the first ``load_plugins()`` of the process happened
    inside a test, that test's teardown rolled the registry back to before the
    plugins registered and nothing ever registered them again -- every later
    test saw a plugin that is loaded and enabled but whose I/O and entity
    declarations had silently vanished.

    That is why ``plugins/tidy/tests/test_smoke.py::test_plugin_loads_and_registers``
    passes alone and fails when another tidy test runs first. Loading here, at
    session scope, puts the plugin registrations *inside* every per-test
    snapshot, so restoring a snapshot preserves them while still dropping
    anything an individual test added.

    Guarded: the headless CI tier has no PyQt5, so plugin import can fail there.
    That tier simply runs without plugins, exactly as before.
    """
    try:
        from plugins.manager import load_plugins
        load_plugins()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_process_singletons(_plugins_loaded_before_isolation):
    """Undo the process-wide state a test can leave behind.

    Fio keeps several deliberate singletons: the plugin manager, the I/O
    revision counter and its cached reverse index, ``IO_REGISTRY`` (the
    process-wide schema of every entity type's inputs and outputs), and
    ``Thing._counters`` (which is what makes entity names unique).  A test that
    adds a connection, registers an entity type or places an entity changes what
    the *next* test sees, which is exactly the kind of order dependence that
    makes a suite untrustworthy.  Everything here is restored, not merely
    cleared, so a test that deliberately sets one of them up still sees its own
    value.

    ``IO_REGISTRY`` matters more than it looks: several tests register a fake
    entity type to exercise the registration path, and a leftover fake is a type
    declaring inputs that nothing implements — which is precisely what the I/O
    conformance tests exist to catch, so a leak there shows up as a failure
    about Fio's own entities.
    """
    io_system = sys.modules.get("editor.io_system")
    saved_rev = getattr(io_system, "_io_revision", None) if io_system else None
    saved_registry = dict(io_system.IO_REGISTRY) if io_system else None
    counters = None
    things = sys.modules.get("editor.things")
    if things is not None:
        counters = dict(things.Thing._counters)
    plugin_state = _snapshot_plugin_state()

    yield

    _restore_plugin_state(plugin_state)

    io_system = sys.modules.get("editor.io_system")
    if io_system is not None:
        # Always invalidate the cached reverse index: it is keyed partly by
        # ``id(list)``, and a freed list's address is readily reused.
        io_system._target_index_cache = None
        io_system._target_index_key = None
        if saved_rev is not None:
            io_system._io_revision = saved_rev
        if saved_registry is not None:
            io_system.IO_REGISTRY.clear()
            io_system.IO_REGISTRY.update(saved_registry)
            # Derived from the registry, so it has to go back with it.
            if hasattr(io_system, "_declared_outputs_cache"):
                io_system._declared_outputs_cache.clear()
    things = sys.modules.get("editor.things")
    if things is not None and counters is not None:
        things.Thing._counters.clear()
        things.Thing._counters.update(counters)


def _snapshot_plugin_state():
    """Which plugins are on, and which of those a level switched on.

    The plugin manager is a process-wide singleton by design, so a test that
    enables a plugin (or loads a map that auto-enables one) changes what every
    later test sees.  Only the *toggles* are snapshotted - the loaded plugin
    list itself is expensive to rebuild and is meant to be shared.
    """
    manager_module = sys.modules.get("plugins.manager")
    if manager_module is None:
        return None
    try:
        manager = manager_module.get_manager()
    except Exception:  # pragma: no cover - defensive
        return None
    return (manager,
            {plugin: bool(getattr(plugin, "enabled", True))
             for plugin in manager.plugins},
            set(manager._auto_enabled))


def _restore_plugin_state(snapshot):
    if snapshot is None:
        return
    manager, enabled, auto_enabled = snapshot
    for plugin, was_enabled in enabled.items():
        plugin.enabled = was_enabled
    manager._auto_enabled.clear()
    manager._auto_enabled.update(auto_enabled)


# ---------------------------------------------------------------------------
# Qt
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qt_app():
    """The one ``QApplication`` for the whole session.

    Qt allows exactly one per process and destroying it invalidates every
    widget built against it, so this is session-scoped and never torn down.
    """
    pytest.importorskip("PyQt5", reason="PyQt5 is not installed")
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# OpenGL tier
# ---------------------------------------------------------------------------

_GL_PROBE = None


def gl_availability():
    """``(ok, reason)`` for whether a real GL context can be created here.

    Probed once per process, in a child-free way: creating and destroying a
    hidden ``QOpenGLContext`` against an offscreen surface is what the visual
    tier itself does, so a successful probe means the tier can actually run.
    """
    global _GL_PROBE
    if _GL_PROBE is not None:
        return _GL_PROBE
    try:
        from PyQt5.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
        from PyQt5.QtWidgets import QApplication
    except Exception as exc:
        _GL_PROBE = (False, "PyQt5 OpenGL bindings unavailable: %s" % exc)
        return _GL_PROBE
    app = QApplication.instance() or QApplication([])
    platform = app.platformName()
    if platform == "offscreen":
        _GL_PROBE = (False,
                     "the Qt platform plugin in use is 'offscreen', which cannot "
                     "create an OpenGL context - run under a display "
                     "(xvfb-run), and make sure nothing has set "
                     "QT_QPA_PLATFORM=offscreen for the session")
        return _GL_PROBE
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    surface = QOffscreenSurface()
    surface.setFormat(fmt)
    surface.create()
    if not surface.isValid():
        _GL_PROBE = (False, "no usable offscreen surface (no GPU/driver in this environment)")
        return _GL_PROBE
    ctx = QOpenGLContext()
    ctx.setFormat(fmt)
    if not ctx.create():
        _GL_PROBE = (False, "QOpenGLContext.create() failed (no GL driver)")
        return _GL_PROBE
    if not ctx.makeCurrent(surface):
        _GL_PROBE = (False, "could not make a GL context current")
        return _GL_PROBE
    ctx.doneCurrent()
    _GL_PROBE = (True, "")
    return _GL_PROBE


@pytest.fixture(autouse=True)
def _skip_without_gl(request):
    """Skip ``gl``-marked tests with a readable reason instead of erroring."""
    if request.node.get_closest_marker("gl") is None:
        return
    ok, reason = gl_availability()
    if not ok:
        pytest.skip("OpenGL tier unavailable: %s" % reason)
