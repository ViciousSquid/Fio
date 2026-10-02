"""One test per defect this suite found, stating the invariant that was broken.

Each entry names what went wrong, why it mattered in the running editor or
game, and then asserts the *property* rather than replaying the reproduction —
a test that only checks "this exact sequence no longer crashes" stops being
useful the moment the code is restructured.
"""

import math
import sys

import numpy as np
import pytest

from engine import brush_geometry as bg
from tests.helpers.worlds import box_brush

pytestmark = []


# ---------------------------------------------------------------------------
# Undo stepped back two gestures at a time
# ---------------------------------------------------------------------------

@pytest.mark.qt
def test_undo_steps_back_exactly_one_operation():
    """``EditorState.undo`` popped a checkpoint and restored the one *below* it.

    Tools checkpoint at the start of a gesture, so the popped entry already is
    the state to return to.  Restoring its predecessor instead meant a mapper
    who made three edits and pressed undo once lost two of them - and the work
    in between was not on the redo stack under the edit they expected either.

    The invariant: undoing N times and redoing N times visits the same states in
    reverse and forward order, one operation at a time.
    """
    pytest.importorskip("PyQt5")
    from editor.editor_state import EditorState

    state = EditorState()
    state.brushes = [box_brush("wall", (0, 0, 0), (64, 64, 64))]
    state.save_state()

    widths = [128.0, 192.0, 256.0]
    for width in widths:
        state.save_state()                       # checkpoint at "mouse-down"
        state.brushes[0]["size"] = [width, 64.0, 64.0]

    walked_back = []
    for _ in widths:
        assert state.undo(), "ran out of history with edits still to undo"
        walked_back.append(state.brushes[0]["size"][0])

    assert walked_back == [192.0, 128.0, 64.0], (
        "undo visited widths %s; each press should step back exactly one edit, "
        "giving 192 -> 128 -> 64" % (walked_back,))

    walked_forward = []
    for _ in widths:
        assert state.redo(), "redo ran out with edits still to re-apply"
        walked_forward.append(state.brushes[0]["size"][0])

    assert walked_forward == [128.0, 192.0, 256.0], (
        "redo visited widths %s; it should retrace the same steps forward"
        % (walked_forward,))


# ---------------------------------------------------------------------------
# NaN from a degenerate ground direction
# ---------------------------------------------------------------------------

@pytest.mark.qt
def test_a_ground_monster_directly_below_its_target_does_not_produce_nan():
    """``glm.normalize`` of the zero vector is NaN, not an error.

    A ground monster flattens its 3D direction to XZ before walking.  With the
    target directly overhead - a flying player above a grunt, or a monster on
    the player's own column - the flattened vector is zero, and the NaN flowed
    into the monster's position and then into ``SpatialGrid.overlaps_wall``,
    which raised ``ValueError: cannot convert float NaN to integer`` on the AI
    thread.  That thread has no exception guard, so every monster in the level
    silently stopped.

    The invariant: a monster's position is always finite.
    """
    pytest.importorskip("PyQt5")
    from editor.things import Monster
    from engine.monster_ai import MonsterAI
    from tests.helpers.fakes import FakeLogicThread, FakePlayer
    from tests.helpers.worlds import make_thing

    ground = [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    monster = make_thing(Monster, "grunt", (0.0, 96.0, 0.0), awake=True)
    logic = FakeLogicThread(brushes=ground, things=[monster],
                            player=FakePlayer((0.0, 900.0, 0.0)))
    logic._monster_things = [monster]
    ai = MonsterAI(logic)
    ai.set_spatial_grid(logic.build_spatial_grid())

    for _ in range(10):
        ai.update(1.0 / 30.0)          # must not raise

    assert all(math.isfinite(value) for value in monster.pos), (
        "the monster's position went non-finite: %s" % (monster.pos,))


@pytest.mark.qt
def test_a_patrol_node_directly_overhead_does_not_produce_nan():
    """The same hazard on the patrol path."""
    pytest.importorskip("PyQt5")
    from editor.things import Monster, PathNode
    from engine.monster_ai import MonsterAI
    from tests.helpers.fakes import FakeLogicThread, FakePlayer
    from tests.helpers.worlds import make_thing

    ground = [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    node = make_thing(PathNode, "above", (0.0, 900.0, 0.0), radius=8.0)
    monster = make_thing(Monster, "walker", (0.0, 96.0, 0.0), awake=True,
                         patrol=True, patrol_target="above")
    logic = FakeLogicThread(brushes=ground, things=[monster, node],
                            player=FakePlayer((100000.0, 0.0, 0.0)))
    logic._monster_things = [monster]
    ai = MonsterAI(logic)
    ai.set_spatial_grid(logic.build_spatial_grid())

    for _ in range(10):
        ai.update(1.0 / 30.0)

    assert all(math.isfinite(value) for value in monster.pos), (
        "patrolling toward a node directly overhead produced %s" % (monster.pos,))


# ---------------------------------------------------------------------------
# A one-node patrol route fired its outputs every tick
# ---------------------------------------------------------------------------

@pytest.mark.qt
def test_a_monster_parked_on_its_only_patrol_node_announces_it_once():
    """Advancing a one-node chain lands back on the node it is standing on.

    The advance still fired ``OnMonsterLeft`` and cleared the "at target" flag,
    so the next tick fired ``OnMonsterArrived`` again - a pair of I/O events
    thirty times a second, for ever.  A logic counter wired to that node counted
    thirty arrivals a second.

    The invariant: an entity announces an arrival only when it has actually
    arrived somewhere new.
    """
    pytest.importorskip("PyQt5")
    from editor.things import Monster, PathNode
    from engine.monster_ai import MonsterAI
    from tests.helpers.fakes import (FakeLogicThread, FakePlayer,
                                     RecordingIOManager)
    from tests.helpers.worlds import make_thing

    ground = [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    node = make_thing(PathNode, "only_node", (0.0, 96.0, 0.0), radius=64.0)
    monster = make_thing(Monster, "walker", (0.0, 96.0, 0.0), awake=True,
                         patrol=True, patrol_target="only_node")
    io_manager = RecordingIOManager()
    logic = FakeLogicThread(brushes=ground, things=[monster, node],
                            player=FakePlayer((100000.0, 0.0, 0.0)),
                            io_manager=io_manager)
    logic._monster_things = [monster]
    ai = MonsterAI(logic)
    ai.set_spatial_grid(logic.build_spatial_grid())

    for _ in range(60):                         # two seconds of ticks
        ai.update(1.0 / 30.0)

    arrivals = io_manager.names().count("OnMonsterArrived")
    departures = io_manager.names().count("OnMonsterLeft")
    assert arrivals == 1, (
        "the monster arrived once and never left, but OnMonsterArrived fired "
        "%d times over 60 ticks" % arrivals)
    assert departures == 0, (
        "OnMonsterLeft fired %d times for a monster that never left its node"
        % departures)


# ---------------------------------------------------------------------------
# Re-entrant plugin discovery dropped a plugin permanently
# ---------------------------------------------------------------------------

def test_discovery_reached_during_a_plugins_own_import_does_not_drop_it():
    """A plugin package whose import calls back into the host.

    ``plugins.tidy`` imports the editor for its entity base, and the editor
    package's initialiser calls ``load_plugins()``.  Discovery then reached
    ``plugins.tidy`` while it was still being imported, got the
    partially-initialised module (no ``PLUGIN`` yet), skipped it - and marked
    itself loaded, so the plugin was gone for the life of the process.

    The invariant: a plugin that could not be read because it was mid-import is
    retried, not dropped.
    """
    from plugins.manager import PluginManager

    manager = PluginManager()

    class _Spec:
        _initializing = True

    class _HalfImported:
        __spec__ = _Spec()

    real_import = __import__

    def _import_module(name):
        return _HalfImported()

    original = sys.modules["plugins.manager"].importlib.import_module
    sys.modules["plugins.manager"].importlib.import_module = _import_module
    try:
        manager._load_one("half_imported")
    finally:
        sys.modules["plugins.manager"].importlib.import_module = original

    assert "half_imported" in manager._deferred, (
        "a mid-import plugin must be recorded for retry; deferred set is %s"
        % (manager._deferred,))
    assert manager._loaded is False, (
        "the manager marked itself fully loaded with a plugin still deferred, "
        "so the retry would never happen")


def test_a_retried_discovery_pass_does_not_re_import_what_already_loaded():
    """The other half of the retry: the second pass must not double-register.

    The invariant: a package already loaded is skipped outright on a retry, so
    its ``register()`` runs exactly once however many passes it takes to finish
    discovery.
    """
    import pkgutil

    import plugins.manager as manager_module
    from plugins.manager import PluginManager

    manager = PluginManager()
    manager._loaded_modules.add("already_loaded")

    attempted = []
    real_iter = manager_module.pkgutil.iter_modules
    real_import = manager_module.importlib.import_module

    def _iter(paths):
        return [pkgutil.ModuleInfo(None, "already_loaded", True)]

    def _import(name, *args, **kwargs):
        attempted.append(name)
        raise AssertionError("an already-loaded plugin was imported again")

    manager_module.pkgutil.iter_modules = _iter
    manager_module.importlib.import_module = _import
    try:
        manager.discover_and_load()
    finally:
        manager_module.pkgutil.iter_modules = real_iter
        manager_module.importlib.import_module = real_import

    assert attempted == [], (
        "discovery re-imported %s even though it was already loaded" % (attempted,))
    assert manager.plugins == [], (
        "the already-loaded package was registered a second time")


# ---------------------------------------------------------------------------
# The engine could not run without PyQt5
# ---------------------------------------------------------------------------

def test_the_monster_ai_module_does_not_require_the_editor_package():
    """``engine.monster_ai`` imported ``editor.debug_console`` unguarded.

    Every other engine module guards that import, because the debug console is
    a Qt widget and the standalone player has no editor package at all.  The AI
    only wants somewhere to write a line, so one unguarded import made the whole
    engine - and this suite's AI tests - depend on PyQt5.

    The invariant: importing the engine's simulation code pulls in no editor
    package and no Qt.
    """
    import subprocess

    from tests.helpers.paths import REPO_ROOT

    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('editor', 'PyQt5'):\n"
        "            raise ModuleNotFoundError('blocked: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import engine.monster_ai\n"
        "assert 'PyQt5' not in sys.modules\n"
        "print('OK')\n" % REPO_ROOT)
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, (
        "engine.monster_ai could not be imported without the editor package:\n"
        "%s%s" % (result.stdout[-600:], result.stderr[-600:]))


# ---------------------------------------------------------------------------
# The player's documented camera math was missing
# ---------------------------------------------------------------------------

def test_the_players_matrix_helpers_provide_the_camera_math_they_document():
    """``player.render.glmath`` documents "a view/projection matrix"...

    ...and shipped only the 2D overlay half, so the player's own 3D pass had
    nothing to build a camera from.

    The invariant: the helpers agree with the engine's conventions - Y up, a
    perspective divide row, and yaw -90 looking down -Z.
    """
    numpy = pytest.importorskip("numpy")
    from player.render import glmath

    projection = glmath.perspective(math.radians(70.0), 16.0 / 9.0, 1.0, 1000.0)
    assert projection.shape == (4, 4)
    assert projection[3, 2] == -1.0, (
        "row 3 of the projection is %s; a perspective matrix needs the -1 that "
        "performs the divide" % (projection[3],))
    assert projection[3, 3] == 0.0

    eye = [10.0, 5.0, 3.0]
    view = glmath.look_at(eye, [10.0, 5.0, -100.0])
    origin = view @ numpy.array(eye + [1.0], dtype=numpy.float32)
    assert numpy.allclose(origin[:3], [0, 0, 0], atol=1e-4), (
        "the eye point maps to %s in view space, not the origin" % (origin[:3],))

    front = glmath.front_from_angles(-90.0, 0.0)
    assert numpy.allclose(front, [0.0, 0.0, -1.0], atol=1e-5), (
        "yaw -90 should look down -Z (the engine default); it points %s"
        % (front,))


# ---------------------------------------------------------------------------
# A test that needed a GPU to parse a text file
# ---------------------------------------------------------------------------

def test_parsing_a_model_file_needs_no_gl_context():
    """``OBJ`` uploads VBOs in its constructor; ``OBJLoader`` just parses.

    The invariant: reading geometry off disk is a head-less operation.
    """
    pytest.importorskip("OpenGL", reason="engine.obj_loader imports PyOpenGL for "
                        "its GL-ready wrapper; the parser itself needs no context")
    from engine.obj_loader import OBJLoader
    from tests.helpers.paths import repo_path

    loader = OBJLoader()
    assert loader.load(repo_path("plugins", "tidy", "assets", "book.obj")) is True
    assert loader.vertices, "the parsed model has no vertices"
    assert loader.faces, "the parsed model has no faces"
