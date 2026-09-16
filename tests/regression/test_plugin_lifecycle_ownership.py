"""One authoritative path into a plugin's play lifecycle.

``PluginManager`` owns plugin discovery, enabling and lifecycle dispatch.  If
generic engine code *also* reached into a named plugin — a
``_start_world_streaming()`` on the logic thread calling ``bigworld``'s
``on_play_start`` directly, say — that plugin's session would be built twice for
one press of Play: two sessions over one world, the second capturing its
"pristine" base from a world the first had already parked, the first left
running with nobody holding a reference to stop it.  The symptom is not a
crash; it is a save that quietly loses everything the player did.

Two halves, because either alone is insufficient:

* a **structural** check that the engine does not name a plugin's lifecycle at
  all, which is what stops the second path being added back; and
* a **behavioural** check that counts real dispatches across real play
  start/stop cycles, which is what proves the one remaining path fires once.

The behavioural half uses Big World because it is the plugin with per-session
state worth corrupting, but nothing it asserts is Big World's: it counts hook
calls and checks the world is handed back unmarked.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Modules that are generic engine/editor infrastructure: they may know that
#: plugins exist, and must not know which ones.
_GENERIC_SOURCES = ("engine/logic_thread.py", "engine/savegame.py",
                    "engine/qt_game_view.py", "engine/spatial.py",
                    "engine/physics.py")

#: The lifecycle hooks ``PluginManager`` owns the dispatch of.  ``register`` and
#: ``connect`` are deliberately not listed: both are ordinary English that Qt
#: and the engine use for their own unrelated calls, so scanning for them by
#: name would flag signal wiring rather than a lifecycle path.
_LIFECYCLE_HOOKS = ("on_play_start", "on_play_stop", "on_tick")

#: The plugin *framework* — the manager, the API, the host, the packaging and
#: integration layers.  Engine code is allowed to depend on these; they are how
#: it hosts plugins at all.  Anything else under ``plugins.`` is a particular
#: plugin's implementation.
_FRAMEWORK_MODULES = frozenset({
    "plugins", "plugins.api", "plugins.manager", "plugins.host",
    "plugins.integration", "plugins.packaging", "plugins.entitybase",
})


def _source(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Structural: only the manager dispatches a lifecycle hook
# ---------------------------------------------------------------------------

def test_engine_code_never_calls_a_plugin_lifecycle_hook_itself():
    """``dispatch_play_start`` is the only way in, from engine code.

    Scans for a call to any lifecycle hook by name.  Finding one in generic
    engine code means a second path exists, which is the defect whether or not
    it currently double-fires.
    """
    offenders = []
    for rel in _GENERIC_SOURCES:
        tree = ast.parse(_source(rel), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else \
                getattr(func, "id", None)
            if name in _LIFECYCLE_HOOKS:
                offenders.append("%s:%d calls %s()" % (rel, node.lineno, name))
    assert offenders == [], (
        "generic engine code invokes a plugin lifecycle hook directly:\n  %s\n"
        "PluginManager owns lifecycle dispatch; a second path means a plugin's "
        "per-session state is built more than once per play session."
        % "\n  ".join(offenders))


def test_the_engine_never_imports_a_specific_plugin():
    """Engine code may host plugins; it may not depend on one.

    The plugin *framework* is fair game — hosting plugins is the engine's job.
    A particular plugin's package is not.  ``engine.spatial`` deliberately
    *names* Big World in prose and owns the parking-marker keys a streaming
    layer writes: that is the engine defining a convention, not importing an
    implementation.  An actual import would make an ordinary map pay for the
    streaming runtime on every load, which is the cost the whole lazy-import
    arrangement in ``plugins/bigworld/plugin.py`` exists to avoid.
    """
    offenders = []
    for rel in _GENERIC_SOURCES:
        tree = ast.parse(_source(rel), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.startswith("plugins.") and name not in _FRAMEWORK_MODULES:
                    offenders.append("%s:%d imports %s" % (rel, node.lineno, name))
    assert offenders == [], (
        "engine code imports a plugin implementation:\n  %s" % "\n  ".join(offenders))


def test_the_manager_is_the_only_caller_of_the_lifecycle_dispatchers():
    """Sanity check on the other side: the single path really is the manager's."""
    src = _source("plugins/manager.py")
    for hook in ("on_play_start", "on_play_stop"):
        assert "plugin.%s(logic)" % hook in src, (
            "PluginManager no longer dispatches %s; the single authoritative "
            "path has moved and this test no longer guards anything" % hook)


# ---------------------------------------------------------------------------
# Behavioural: one dispatch per play session, over repeated cycles
# ---------------------------------------------------------------------------

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import PlayerStart                     # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from plugins.bigworld.entities import BigWorldSettings    # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402


class _Counter:
    """Counts lifecycle dispatches without changing what they do."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.starts = []
        self.stops = []
        self._real_start = plugin.on_play_start
        self._real_stop = plugin.on_play_stop
        plugin.on_play_start = self._start
        plugin.on_play_stop = self._stop

    def _start(self, logic):
        self.starts.append(id(logic))
        return self._real_start(logic)

    def _stop(self, logic):
        self.stops.append(id(logic))
        return self._real_stop(logic)

    def release(self):
        del self.plugin.on_play_start
        del self.plugin.on_play_stop


@pytest.fixture
def streaming_session():
    """A Big World map in a real ``LogicThread``, with lifecycle instrumented."""
    made = []

    def _build(bigworld=True):
        state = EditorState()
        state.brushes = [box_brush("near", (0, 64, 0), (128, 128, 128)),
                         box_brush("far", (20000, 64, 0), (128, 128, 128))]
        state.things = [make_thing(PlayerStart, "spawn", (0, 64, 0))]
        if bigworld:
            state.things.insert(0, make_thing(BigWorldSettings, "bw", (0, 0, 0)))
        thread = LogicThread(ThreadedGameState(), state)
        thread.plugins.auto_enable_for_map(state.get_level_data())
        plugin = thread.plugins.find_plugin("bigworld")
        counter = _Counter(plugin)
        made.append((thread, counter))
        thread.player = Player(0.0, 0.0)
        return state, thread, counter

    yield _build

    for thread, counter in made:
        thread.set_play_mode(False)
        counter.release()
        thread.stop()


@pytest.mark.qt
@pytest.mark.integration
def test_one_play_session_starts_the_plugin_exactly_once(streaming_session):
    _state, thread, counter = streaming_session()

    thread.set_play_mode(True)

    assert len(counter.starts) == 1, (
        "on_play_start ran %d times for one press of Play; a second lifecycle "
        "path is building a second session over the same world"
        % len(counter.starts))
    assert getattr(thread, "_bigworld", None) is not None, (
        "fixture: the map should have started a streaming session")


@pytest.mark.qt
@pytest.mark.integration
def test_repeated_play_cycles_pair_one_start_with_one_stop(streaming_session):
    """Start/stop/start/stop, with the world checked after every cycle."""
    state, thread, counter = streaming_session()
    sessions = []

    for cycle in range(3):
        thread.set_play_mode(True)
        sessions.append(id(getattr(thread, "_bigworld", None)))
        assert len(counter.starts) == cycle + 1, (
            "cycle %d: %d starts for %d play sessions"
            % (cycle, len(counter.starts), cycle + 1))

        thread.set_play_mode(False)
        assert len(counter.stops) == cycle + 1, (
            "cycle %d: %d stops for %d play sessions"
            % (cycle, len(counter.stops), cycle + 1))
        assert getattr(thread, "_bigworld", None) is None, (
            "cycle %d: the streaming session outlived play mode" % cycle)

    assert len(set(sessions)) == len(sessions), (
        "two play sessions shared one streaming session object")
    # Nothing of the session may survive into the authored world.
    leaked = [b.get("name") for b in state.brushes
              if "bw_active" in b or "_bw_parked_hidden" in b or "_sim_tier" in b]
    assert leaked == [], (
        "streaming markers left on %s after three play cycles" % (leaked,))
    assert [b.get("hidden") for b in state.brushes] == [False, False], (
        "a brush parked by a play session stayed hidden in the editor world")


@pytest.mark.qt
@pytest.mark.integration
def test_a_map_without_the_settings_entity_starts_no_session(streaming_session):
    """The opt-in gate, on the same instrumented path.

    ``on_play_start`` is still dispatched — the plugin is what decides — but it
    must decline, and decline without importing the streaming runtime.
    """
    _state, thread, counter = streaming_session(bigworld=False)

    thread.set_play_mode(True)

    assert getattr(thread, "_bigworld", None) is None, (
        "a map with no BigWorldSettings entity started a streaming session")
    assert len(counter.starts) <= 1, "on_play_start ran more than once"


@pytest.mark.qt
@pytest.mark.integration
def test_stopping_a_session_that_never_started_is_harmless(streaming_session):
    """Editor startup calls ``set_play_mode(False)`` before any Play is pressed."""
    _state, thread, counter = streaming_session()

    thread.set_play_mode(False)
    thread.set_play_mode(False)

    assert counter.starts == [], "a stop dispatched a start"
    assert getattr(thread, "_bigworld", None) is None
