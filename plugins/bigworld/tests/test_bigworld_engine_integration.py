"""Big World as an engine capability, and absent when it is not needed.

Two things are pinned here.

**Absence.** "Disabled" is not the same as "not there". A map that does not use
Big World must not load its runtime at all: no cell manager, no session, no
streaming state, no import. The test for that has to run in a subprocess,
because once any other test in the session has imported the runtime the module
is in ``sys.modules`` for good.

**Shared infrastructure.** Big World streams the same 512-unit columns
:class:`engine.physics.SpatialGrid` buckets collision brushes into, and it does
so through the *engine's* implementation of that convention rather than a copy
of its own. If the two ever disagreed about which cell a brush is in, streaming
would activate one set of geometry and collide against another.
"""

import os
import subprocess
import sys
import textwrap

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def run_isolated(body):
    """Run a snippet in a fresh interpreter and return what it reported.

    Import-footprint questions cannot be asked inside a test session that has
    already imported the thing being asked about. The snippet reports by calling
    ``report(...)``; everything else Fio prints on the way up is ignored.
    """
    script = "def report(*a): print('RESULT:', *a)\n" + textwrap.dedent(body)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT, capture_output=True, text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode == 0, result.stderr
    lines = [line[len("RESULT:"):].strip()
             for line in result.stdout.splitlines()
             if line.startswith("RESULT:")]
    assert lines, "snippet reported nothing:\n%s" % result.stdout
    return lines[0]


# ---------------------------------------------------------------------------
# Absence for maps that do not use Big World
# ---------------------------------------------------------------------------

RUNTIME_MODULES = ("plugins.bigworld.manager", "plugins.bigworld.runtime",
                   "plugins.bigworld.cell", "plugins.bigworld.persistence",
                   "plugins.bigworld.streaming")


def test_loading_plugins_does_not_import_the_bigworld_runtime():
    out = run_isolated("""
        import sys
        from plugins.manager import load_plugins
        load_plugins()
        report(sorted(m for m in sys.modules if m.startswith('plugins.bigworld')))
    """)
    loaded = eval(out)
    for module in RUNTIME_MODULES:
        assert module not in loaded, "%s was imported for a plugin load" % module


def test_a_map_with_no_bigworld_entity_never_starts_a_session():
    out = run_isolated("""
        import sys
        from plugins.manager import load_plugins, get_manager
        load_plugins()
        plugin = next(p for p in get_manager().plugins if p.name == 'bigworld')

        class Logic:
            things = []
            brushes = []

        logic = Logic()
        plugin.on_play_start(logic)
        plugin.on_tick(logic, None)
        plugin.on_play_stop(logic)
        report(repr(getattr(logic, '_bigworld', 'missing')),
               sorted(m for m in sys.modules if m.startswith('plugins.bigworld')))
    """)
    session_repr, loaded = out.split(' ', 1)
    assert session_repr == 'None'
    loaded = eval(loaded)
    for module in RUNTIME_MODULES:
        assert module not in loaded, "%s was imported for an ordinary map" % module


def test_the_opt_in_test_itself_costs_no_import():
    """``map_uses_bigworld`` is the gate, so it must not pull the runtime in."""
    out = run_isolated("""
        import sys
        from plugins.bigworld.plugin import BigWorldPlugin
        assert BigWorldPlugin.map_uses_bigworld([]) is False
        assert BigWorldPlugin.map_uses_bigworld(
            [{'type': 'bigworldsettings'}]) is True
        assert BigWorldPlugin.map_uses_bigworld(
            [{'properties': {'type': 'BigWorld_Settings'}}]) is True
        report(sorted(m for m in sys.modules if m.startswith('plugins.bigworld')))
    """)
    loaded = eval(out)
    for module in RUNTIME_MODULES:
        assert module not in loaded


def test_a_map_with_a_bigworld_entity_activates_streaming():
    out = run_isolated("""
        import sys
        from plugins.manager import load_plugins, get_manager
        load_plugins()
        manager = get_manager()
        plugin = next(p for p in manager.plugins if p.name == 'bigworld')

        from plugins.bigworld.entities import BigWorldSettings
        settings = BigWorldSettings(pos=[0, 0, 0])

        class Logic:
            things = [settings]
            brushes = [{'id': 'a', 'pos': [0, 0, 0], 'size': [64, 64, 64]},
                       {'id': 'b', 'pos': [20000, 0, 0], 'size': [64, 64, 64]}]
            player = None

        logic = Logic()
        plugin.on_play_start(logic)
        session = logic._bigworld
        report(session is not None,
               session.manager.stats()['total_brushes'],
               'plugins.bigworld.runtime' in sys.modules)
        plugin.on_play_stop(logic)
    """)
    started, total, imported = out.split()
    assert started == 'True'
    assert int(total) == 2
    assert imported == 'True'


def test_stopping_a_session_leaves_the_world_exactly_as_it_was():
    from plugins.bigworld.runtime import BigWorldSession

    far = {'id': 'far', 'pos': [30000, 0, 0], 'size': [64, 64, 64]}
    near = {'id': 'near', 'pos': [0, 0, 0], 'size': [64, 64, 64], 'hidden': True}

    class Logic:
        things = []
        player = None

    logic = Logic()
    logic.brushes = [near, far]
    session = BigWorldSession(logic, activation_radius=1024.0,
                              deactivation_radius=1200.0)
    session.start(player_pos=(0.0, 0.0, 0.0))
    session.stop()

    assert near.get('hidden') is True        # the mapper hid it; still hidden
    assert far.get('hidden') in (False, None)
    for brush in (near, far):
        assert not [k for k in brush if k.startswith('_bw_')]


# ---------------------------------------------------------------------------
# One grid convention, shared with the engine
# ---------------------------------------------------------------------------

def test_bigworld_reads_the_cell_maths_from_the_engine():
    from engine import spatial
    from plugins.bigworld import cell

    assert cell.CELL_SIZE is spatial.CELL_SIZE
    assert cell.cell_of_point is spatial.cell_of_point
    assert cell.cells_for_aabb is spatial.cells_for_aabb
    assert cell.cell_distance_sq is spatial.cell_distance_sq
    assert cell.cells_within is spatial.cells_within


def test_the_collision_grid_and_bigworld_agree_on_every_brush():
    """The point of sharing: one brush, one answer about which cells it is in."""
    from engine.physics import SpatialGrid
    from plugins.bigworld.manager import BigWorldManager

    brushes = [
        {'id': 'origin', 'pos': [0, 0, 0], 'size': [64, 64, 64]},
        {'id': 'spanning', 'pos': [500, 0, 500], 'size': [512, 64, 512]},
        {'id': 'negative', 'pos': [-900, 0, -30], 'size': [128, 64, 128]},
        {'id': 'boundary', 'pos': [512, 0, 512], 'size': [4, 64, 4]},
        {'id': 'huge', 'pos': [0, 0, 0], 'size': [4096, 64, 4096]},
    ]

    grid = SpatialGrid()
    grid.populate(brushes)

    manager = BigWorldManager()
    manager.index_world(brushes, [])

    for brush in brushes:
        in_grid = {coord for coord, bucket in grid.cells.items()
                   if any(b is brush for b in bucket)}
        in_bigworld = {coord for coord, cell in manager.cells.items()
                       if any(b is brush for b in cell.brushes)}
        assert in_grid == in_bigworld, brush['id']


def test_the_collision_grid_keeps_parked_brushes():
    """Parking is a render flag; the grid outlives it and must not be fooled.

    Rebuilding the grid mid-play (a model-collision toggle) used to drop every
    brush Big World had parked, permanently — even once its cell came back.
    """
    from engine.physics import SpatialGrid
    from engine.spatial import PARKED_HIDDEN_KEY

    parked = {'id': 'parked', 'pos': [0, 0, 0], 'size': [64, 64, 64],
              'hidden': True, PARKED_HIDDEN_KEY: False}
    authored = {'id': 'authored', 'pos': [128, 0, 0], 'size': [64, 64, 64],
                'hidden': True}

    grid = SpatialGrid()
    grid.populate([parked, authored])
    filed = [b for bucket in grid.cells.values() for b in bucket]

    assert any(b is parked for b in filed)
    assert not any(b is authored for b in filed)


def test_a_brush_with_no_uuid_is_still_streamed():
    """start() parks every brush, so one the index refused would vanish."""
    from plugins.bigworld.manager import BigWorldManager

    with_id = {'id': 'A', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    without_id = {'pos': [16, 0, 0], 'size': [64, 64, 64]}

    manager = BigWorldManager(activation_radius=1024.0,
                              deactivation_radius=1200.0)
    manager.index_world([with_id, without_id], [])
    manager.update((0.0, 0.0, 0.0), force=True)

    active = manager.active_brushes()
    assert any(b is with_id for b in active)
    assert any(b is without_id for b in active)
    assert manager.is_brush_active(without_id)


def test_activation_stays_spatially_correct_as_the_player_moves():
    from plugins.bigworld.manager import BigWorldManager

    near = {'id': 'near', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    far = {'id': 'far', 'pos': [8192, 0, 0], 'size': [64, 64, 64]}

    manager = BigWorldManager(activation_radius=1024.0,
                              deactivation_radius=1200.0)
    manager.index_world([near, far], [])

    manager.update((0.0, 0.0, 0.0), force=True)
    assert manager.is_brush_active(near)
    assert not manager.is_brush_active(far)

    manager.update((8192.0, 0.0, 0.0), force=True)
    assert manager.is_brush_active(far)
    assert not manager.is_brush_active(near)

    manager.update((0.0, 0.0, 0.0), force=True)
    assert manager.is_brush_active(near)
    assert not manager.is_brush_active(far)


def test_hysteresis_still_holds_after_the_radius_query_moved_to_the_engine():
    """A player loitering on a boundary must not thrash cells on and off."""
    from plugins.bigworld.manager import BigWorldManager

    brushes = [{'id': str(i), 'pos': [i * 512, 0, 0], 'size': [64, 64, 64]}
               for i in range(8)]
    manager = BigWorldManager(activation_radius=1024.0,
                              deactivation_radius=1536.0)
    manager.index_world(brushes, [])
    manager.update((0.0, 0.0, 0.0), force=True)
    active = set(manager.active_cells)

    # Step just past a cell boundary and back: nothing inside the hysteresis
    # band may be dropped.
    manager.update((513.0, 0.0, 0.0), force=True)
    assert active <= set(manager.active_cells)


def test_a_brush_spanning_cells_stays_active_while_any_of_them_is():
    from plugins.bigworld.manager import BigWorldManager

    spanning = {'id': 'span', 'pos': [512, 0, 0], 'size': [2048, 64, 64]}
    manager = BigWorldManager(activation_radius=600.0,
                              deactivation_radius=600.0)
    manager.index_world([spanning], [])

    manager.update((0.0, 0.0, 0.0), force=True)
    assert manager.is_brush_active(spanning)
    manager.update((1024.0, 0.0, 0.0), force=True)
    assert manager.is_brush_active(spanning)
