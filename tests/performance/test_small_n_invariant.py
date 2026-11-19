"""Invariant: small N stays a Python loop.

NumPy has a fixed cost a call -- a few microseconds of dispatch, allocation
and conversion -- paid however few the rows. A loop costs per row. So for a
path whose input is usually small (a handful of emissive decals, a few
moving doors, the candidates one ray meets) a plain Python loop is the
faster one, often by several times, and "vectorising everything" makes it
slower. Bulk is where NumPy wins; small N is not bulk.

Such a path therefore keeps **both** implementations and switches between
them at a **crossover**: a named constant, the count from which the NumPy
path runs. Every crossover

1. is registered in :data:`CROSSOVERS` below (a new one fails
   :func:`test_every_crossover_is_registered` until it is);
2. is documented where it is defined -- a comment that says what is
   measured and why, ideally the measured table and how to re-measure it
   (:func:`test_crossovers_are_documented`);
3. keeps its loop: it is above zero, so below it the loop runs
   (:func:`test_crossovers_keep_their_loop`; zero would mean "always
   NumPy", the regression this module exists to stop);
4. answers the same either side of it -- both paths are held to identical
   results by a test (for the emissive decals, below; the others' are named
   in their registry entries).

To re-measure a crossover, run its threshold benchmark (the benchmark tier:
``--run-benchmarks``), and move the constant to where the paths cross on the
machine that matters. Do not delete the loop because NumPy looks tidier.
"""

import ast
import importlib
import os
import re
import time

import numpy as np
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

#: Every small-N crossover: ``(file, qualified name)`` -> the test that holds
#: its two paths to the same answer. A base-class default of 0 (each
#: subclass sets its own) is listed in :data:`BASE_DEFAULTS` instead.
CROSSOVERS = {
    ("engine/renderer/forward/decals.py", "GLOW_REMAP_VECTOR_FROM"):
        "tests/performance/test_small_n_invariant.py::test_glow_remap_paths_agree",
    ("engine/brush_placement.py", "BulkBoxes.SCALAR_BELOW"):
        "tests/brush_model (BulkBoxes: same values as world_box, to the bit)",
    ("engine/spatial_grid.py", "SpatialGrid.LOS_DENSE_MIN_CANDIDATES"):
        "tests/physics/test_line_of_sight.py",
    ("engine/logic_collision.py", "LogicCollision.DENSE_WALK_MIN_BRUSHES"):
        "tests/performance/test_hitscan_threshold_benchmark.py",
    ("engine/monster_table.py", "MonsterTable.CROSSFIRE_NUMPY_MIN"):
        "tests/performance/test_crossfire_crossover_benchmark.py",
    ("engine/physics/soma.py", "Soma.FLOOR_BATCH_MIN_BODIES"):
        "tests/physics/test_dynamic_bodies.py",
    ("engine/physics/soma.py", "Soma.FLOOR_BATCH_MIN_POINTS"):
        "tests/physics/test_rolling_ball.py",
    ("engine/mover_table.py", "LinearMovers.SETTLE_ROWS_BELOW"):
        "tests/performance/test_mover_threshold_benchmark.py",
    ("engine/mover_table.py", "LinearMovers.WALK_ROWS_BELOW"):
        "tests/performance/test_mover_threshold_benchmark.py",
    ("engine/mover_table.py", "Doors.WALK_ROWS_BELOW"):
        "tests/performance/test_mover_threshold_benchmark.py",
    ("engine/mover_table.py", "MoverTable.PUBLISH_ROWS_BELOW"):
        "tests/performance/test_mover_threshold_benchmark.py",
    ("engine/terrain.py", "Terrain.STAMP_VECTOR_MIN"):
        "tests/performance/test_terrain_kernel_benchmark.py",
    ("engine/renderer/forward/explosion_rows.py", "EXPLOSION_ROWS_VECTOR_FROM"):
        "tests/engine/test_explosion.py::test_explosion_row_paths_agree",
    ("engine/entity_table.py", "EXPLOSION_LIGHT_VECTOR_FROM"):
        "tests/engine/test_explosion.py::test_explosion_light_paths_agree",
    ("engine/logic_interaction.py", "LEVELCHANGER_VECTOR_FROM"):
        "tests/performance/test_levelchanger_crossover_benchmark.py"
        "::test_the_loop_and_numpy_find_the_same_level_changer",
}

#: Base-class defaults that subclasses override (0: the base plans always).
BASE_DEFAULTS = {
    ("engine/mover_table.py", "_Group.WALK_ROWS_BELOW"),
}

#: What a crossover is called: ``..._BELOW`` (the loop below it) or a
#: ``..._MIN`` / ``..._FROM`` naming the NumPy, dense, batched or vector
#: path that runs from it.
CROSSOVER_NAME = re.compile(
    r"(?:^|_)(?:ROWS_BELOW|SCALAR_BELOW|NUMPY_MIN|DENSE_(?:WALK_)?MIN|"
    r"VECTOR_(?:FROM|MIN)|BATCH_MIN)(?:_|$)")

#: Where crossovers live.
SCANNED = ("engine", "editor", "player")


def _assignments(path):
    """``{qualified name: (line, comment lines above)}`` of the module- and
    class-level assignments in *path*."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        source = handle.read()
    lines = source.splitlines()
    found = {}

    def comments_above(lineno):
        out = []
        i = lineno - 2
        while i >= 0 and lines[i].strip().startswith("#"):
            out.append(lines[i].strip())
            i -= 1
        return out[::-1]

    def visit(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        found[prefix + target.id] = (node.lineno, comments_above(node.lineno))

    visit(ast.parse(source).body, "")
    return found


def _python_files():
    for top in SCANNED:
        for folder, dirs, files in os.walk(os.path.join(ROOT, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.relpath(os.path.join(folder, name), ROOT).replace(os.sep, "/")


def _value(path, qualname):
    module = importlib.import_module(path[:-3].replace("/", "."))
    value = module
    for part in qualname.split("."):
        value = getattr(value, part)
    return value


def test_every_crossover_is_registered():
    known = set(CROSSOVERS) | BASE_DEFAULTS
    unregistered = []
    for path in _python_files():
        for qualname in _assignments(path):
            if CROSSOVER_NAME.search(qualname.rsplit(".", 1)[-1]) and (path, qualname) not in known:
                unregistered.append(f"{path}: {qualname}")
    assert not unregistered, (
        "small-N crossovers not in CROSSOVERS (document, register, and hold "
        "both paths to the same answer): %s" % unregistered)


def test_registered_crossovers_exist():
    missing = [f"{path}: {name}" for path, name in sorted(CROSSOVERS | dict.fromkeys(BASE_DEFAULTS))
               if name not in _assignments(path)]
    assert not missing, ("a registered crossover is gone -- was its loop deleted? "
                         "Small N stays a loop (see this module): %s" % missing)


@pytest.mark.parametrize("path,qualname", sorted(CROSSOVERS))
def test_crossovers_keep_their_loop(path, qualname):
    pytest.importorskip("numpy")
    if path.startswith("engine/renderer"):
        pytest.importorskip("OpenGL")
    value = _value(path, qualname)
    assert isinstance(value, (int, float)) and value > 0, (
        f"{path}: {qualname} = {value!r}: a crossover of 0 runs NumPy for every "
        "count, however small -- the regression this invariant stops")


@pytest.mark.parametrize("path,qualname", sorted(CROSSOVERS))
def test_crossovers_are_documented(path, qualname):
    _line, comments = _assignments(path)[qualname]
    text = " ".join(c.lstrip("#: ").strip() for c in comments)
    assert len(text) >= 40, (
        f"{path}: {qualname} needs a comment above it saying what the two "
        "paths cost and where they cross (measured, and how to re-measure)")


# ---------------------------------------------------------------------------
# The emissive decals' layer remap (engine/renderer/forward/decals.py)
# ---------------------------------------------------------------------------

def _glow_case(n, seed=0):
    rng = np.random.default_rng(seed)
    index = {3: 0, 7: 1, 9: 2, 12: 3}
    lut = np.full(13, -1, dtype=np.int64)
    for shared, own in index.items():
        lut[shared] = own
    rows = np.zeros((n, 36), dtype=np.float32)
    # Layers in the array, one that is not (5), one past the table (40).
    rows[:, 16] = rng.choice([3, 7, 9, 12, 5, 40], n)
    rows[:, 17] = 1.0
    return rows, index, lut


def test_glow_remap_paths_agree():
    pytest.importorskip("OpenGL")
    from engine.renderer.forward import decals
    edge = decals.GLOW_REMAP_VECTOR_FROM
    for n in (1, 2, 7, edge - 1, edge, edge + 1, 3 * edge):
        rows, index, lut = _glow_case(n, seed=n)
        shared = rows[:, 16].astype(np.int64)
        by_loop, by_table = rows.copy(), rows.copy()
        decals._remap_glow_rows_loop(by_loop, shared.tolist(), index)
        decals._remap_glow_rows_table(by_table, shared, lut)
        assert np.array_equal(by_loop, by_table), n
        assert decals.glow_layers(shared) == sorted(set(shared.tolist()))
        dispatched = rows.copy()
        decals.remap_glow_rows(dispatched, shared, index, lut)
        assert np.array_equal(dispatched, by_loop), n


def test_glow_remap_loops_below_the_crossover(monkeypatch):
    """Below the crossover no NumPy pass runs: a "vectorise it all" change
    fails here."""
    pytest.importorskip("OpenGL")
    from engine.renderer.forward import decals

    def numpy_path(*_args, **_kwargs):
        raise AssertionError("NumPy path taken for a small count")

    monkeypatch.setattr(decals, "_remap_glow_rows_table", numpy_path)
    monkeypatch.setattr(decals.np, "unique", numpy_path)
    for n in (1, 4, decals.GLOW_REMAP_VECTOR_FROM - 1):
        rows, index, lut = _glow_case(n)
        shared = rows[:, 16].astype(np.int64)
        decals.glow_layers(shared)
        decals.remap_glow_rows(rows, shared, index, lut)


@pytest.mark.benchmark
@pytest.mark.slow
def test_glow_remap_threshold_benchmark():
    """The table GLOW_REMAP_VECTOR_FROM's comment quotes: loop against NumPy
    at rising counts. Nothing asserted (a crossover is a measurement about a
    machine, not a defect)."""
    pytest.importorskip("OpenGL")
    from engine.renderer.forward import decals

    def best(fn, repeat=5, number=2000):
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            for _ in range(number):
                fn()
            times.append((time.perf_counter() - t0) / number * 1e6)
        return min(times)

    print("\n decals    loop     NumPy")
    for n in (1, 4, 16, 32, 64, 96, 128, 256, 1024):
        rows, index, lut = _glow_case(n)
        shared = rows[:, 16].astype(np.int64)
        loop = best(lambda: (sorted(set(shared.tolist())),
                             decals._remap_glow_rows_loop(rows.copy(), shared.tolist(), index)))
        table = best(lambda: (np.unique(shared).tolist(),
                              decals._remap_glow_rows_table(rows.copy(), shared, lut)))
        mark = "  <- loop" if loop < table else ""
        print(f" {n:6d} {loop:7.1f} us {table:7.1f} us{mark}")


# ---------------------------------------------------------------------------
# Explosions: their instance rows (engine/renderer/forward/explosion_rows.py)
# and their lights (engine/entity_table.py). Both paths are held to the same
# answers in tests/engine/test_explosion.py.
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
@pytest.mark.slow
def test_explosion_threshold_benchmark():
    """The tables EXPLOSION_ROWS_VECTOR_FROM's and
    EXPLOSION_LIGHT_VECTOR_FROM's comments quote. Nothing asserted."""
    pytest.importorskip("PyQt5")
    pytest.importorskip("OpenGL")
    from editor.things import Effect
    from engine.entity_table import EntityTable, _CLOCK_ORIGIN
    from engine.renderer.forward import explosion_rows as er

    def best(fn, repeat=5, number=2000):
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            for _ in range(number):
                fn()
            times.append((time.perf_counter() - t0) / number * 1e6)
        return min(times)

    print("\n explosions  rows: loop    NumPy   light: loop    NumPy")
    for n in (1, 4, 8, 16, 32, 48, 128):
        things = [Effect(pos=(i * 13.0, 0.0, -i * 7.0),
                         properties={"effect_type": "EXPLOSION", "preview": True})
                  for i in range(n)]
        table = EntityTable()
        table.begin_frame(things, epoch=1, effect_runtime=False)
        slots = np.asarray(table.effect_slots, dtype=np.int32)
        loop = best(lambda: er._rows_loop(table, slots, (5.0, 3.0), _CLOCK_ORIGIN))
        vec = best(lambda: er._rows_table(table, slots, (5.0, 3.0), _CLOCK_ORIGIN))
        e = np.linspace(0, 0.5, n).astype(np.float32)
        life = np.full(n, 0.5, np.float32)
        on = np.ones(n, bool)
        power = np.full(n, 2.5, np.float32)
        light_loop = best(lambda: table._explosion_lights_loop(slots, e, life, on, on, power))
        light_vec = best(lambda: table._explosion_lights_table(slots, e, life, on, on, power))
        print(f" {n:6d}  {loop:9.1f} us {vec:6.1f} us  {light_loop:9.1f} us {light_vec:6.1f} us")
