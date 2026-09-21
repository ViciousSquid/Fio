"""
Synthetic Big World map generator + benchmark harness.

Two jobs, both runnable headless (no editor, engine, PyQt or OpenGL — it drives
the :class:`~plugins.bigworld.manager.BigWorldManager` and
:class:`~plugins.bigworld.runtime.BigWorldSession` directly):

1. **Generate** a Fio map JSON containing a huge field of brushes, some
   entities and lights, and a ``BigWorldSettings`` entity so the map opts into
   streaming. The file loads in ordinary Fio (a map without the plugin still
   opens it — the settings entity is just an inert unknown type there).

2. **Benchmark** the streaming system across the sizes the task calls for
   (10k / 50k / 100k / 250k / 500k brushes) and report the metrics that prove it
   scales with the *local* region, not the world:

       startup (index) time · initial activation time · per-boundary-cross time
       · frame cost while stationary · active vs total brush/entity counts ·
       cost of walking across many cell boundaries · save/load time · memory.

Usage
-----
    python -m plugins.bigworld.tools.generate_world benchmark
    python -m plugins.bigworld.tools.generate_world benchmark --sizes 10000,50000
    python -m plugins.bigworld.tools.generate_world generate --brushes 100000 \\
        --out maps/bigworld_100k.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import tracemalloc
import uuid

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from plugins.bigworld.manager import BigWorldManager
from plugins.bigworld.runtime import BigWorldSession


# ---------------------------------------------------------------------------
# World generation
# ---------------------------------------------------------------------------

def generate_world(n_brushes: int, lattice: float = 192.0,
                   entity_every: int = 40, light_every: int = 120):
    """Build a square field of ``n_brushes`` brushes plus scattered entities.

    Returns ``(brushes, things)`` as the plain dicts / lightweight objects the
    manager consumes. Brushes are laid on a regular lattice so the field spans
    ``sqrt(n) * lattice`` units each way — big enough that only a fraction is
    ever local (e.g. 500k brushes on a 192-unit lattice ≈ a 135k×135k world,
    ~264 cells wide).
    """
    side = int(math.isqrt(n_brushes))
    brushes = []
    things = []
    for i in range(side):
        for j in range(side):
            n = i * side + j
            x = i * lattice
            z = j * lattice
            brushes.append({
                "id": str(uuid.uuid4()),
                "pos": [x, 0.0, z],
                "size": [64.0, 128.0, 64.0],
                "texture": "dev_128",
                "name": f"brush_{n}",
            })
            if entity_every and n % entity_every == 0:
                things.append(_thing(x + 20, 32, z + 20, "monster",
                                     name=f"npc_{n}", health=100))
            if light_every and n % light_every == 0:
                things.append(_thing(x, 96, z, "light",
                                     name=f"light_{n}", radius=384.0,
                                     colour=[255, 220, 180], intensity=1.0))
    # A couple of always-on globals to show persistent entities survive streaming.
    things.append(_thing(0, 0, 0, "worldmanager", name="world_manager"))
    return brushes, things


class _GenThing:
    """A minimal thing (matches the .pos / .properties surface the manager reads)."""

    __slots__ = ("pos", "properties")

    def __init__(self, pos, properties):
        self.pos = pos
        self.properties = properties


def _thing(x, y, z, ttype, **props):
    p = {"id": str(uuid.uuid4()), "type": ttype}
    p.update(props)
    return _GenThing([float(x), float(y), float(z)], p)


def _settings_thing(activation_radius=2048.0, deactivation_radius=2304.0):
    return _thing(0, 0, 0, "bigworldsettings", name="bigworld_settings",
                  enabled=True, activation_radius=activation_radius,
                  deactivation_radius=deactivation_radius, show_cell_debug=True)


def to_map_dict(brushes, things) -> dict:
    """Serialize to Fio's map format (version 3)."""
    return {
        "version": 3,
        "brushes": [dict(b) for b in brushes],
        "things": [
            {"type": t.properties.get("type"), "pos": list(t.pos),
             "properties": {k: v for k, v in t.properties.items()}}
            for t in things
        ],
    }


def write_map(path: str, n_brushes: int, activation_radius=2048.0):
    brushes, things = generate_world(n_brushes)
    things.append(_settings_thing(activation_radius))
    data = to_map_dict(brushes, things)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"Wrote {path}: {len(brushes):,} brushes, {len(things):,} things, "
          f"{size_mb:.1f} MB")
    return path


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------

class _BenchLogic:
    """Stand-in logic object for the session (just brushes/things/player.pos)."""

    class _P:
        def __init__(self, pos):
            self.pos = list(pos)

    def __init__(self, brushes, things, player_pos=(0, 0, 0)):
        self.brushes = brushes
        self.things = things
        self.player = _BenchLogic._P(player_pos)


def _fmt(v):
    return f"{v:,}" if isinstance(v, int) else f"{v:.3f}"


def benchmark_size(n_brushes: int, walk_cells: int = 32,
                   activation_radius: float = 2048.0) -> dict:
    """Run the full streaming benchmark for one world size and return metrics."""
    lattice = 192.0
    side = int(math.isqrt(n_brushes))
    world_span = side * lattice

    tracemalloc.start()
    t0 = time.perf_counter()
    brushes, things = generate_world(n_brushes, lattice=lattice)
    t_generate = time.perf_counter() - t0
    _, mem_after_gen = tracemalloc.get_traced_memory()

    mid = (side // 2) * lattice
    logic = _BenchLogic(brushes, things, player_pos=(mid, 0, mid))
    session = BigWorldSession(logic, activation_radius=activation_radius,
                              deactivation_radius=activation_radius + 256.0)

    # Startup: index + park world + activate the local region.
    t0 = time.perf_counter()
    session.start()
    t_startup = time.perf_counter() - t0
    _, mem_after_start = tracemalloc.get_traced_memory()

    stats = session.stats()

    # Stationary frame cost: manager.update early-outs (same cell), so this is
    # the per-frame cost the running game actually pays when the player is still.
    reps = 2000
    t0 = time.perf_counter()
    for _ in range(reps):
        session.tick()
    t_frame_still = (time.perf_counter() - t0) / reps

    # One boundary crossing (the expensive per-move event).
    logic.player.pos = [mid + 512, 0, mid]
    t0 = time.perf_counter()
    changed = session.tick()
    t_one_cross = time.perf_counter() - t0

    # Walk across many cell boundaries; report the average per-cross cost.
    crosses = 0
    t0 = time.perf_counter()
    for k in range(1, walk_cells + 1):
        logic.player.pos = [mid + 512 * k, 0, mid]
        if session.tick():
            crosses += 1
    t_walk = time.perf_counter() - t0
    t_per_cross = (t_walk / crosses) if crosses else 0.0

    # Save / load round-trip time.
    t0 = time.perf_counter()
    data = to_map_dict(brushes, things)
    blob = json.dumps(data)
    t_save = time.perf_counter() - t0
    t0 = time.perf_counter()
    reloaded = json.loads(blob)
    t_load = time.perf_counter() - t0
    save_mb = len(blob) / (1024 * 1024)

    session.stop()
    tracemalloc.stop()

    return {
        "brushes": n_brushes,
        "world_span": world_span,
        "generate_s": t_generate,
        "startup_s": t_startup,
        "frame_still_us": t_frame_still * 1e6,
        "one_cross_ms": t_one_cross * 1e3,
        "per_cross_ms": t_per_cross * 1e3,
        "active_brushes": stats["active_brushes"],
        "total_brushes": stats["total_brushes"],
        "active_entities": stats["active_entities"],
        "total_entities": stats["total_entities"],
        "active_lights": stats["active_lights"],
        "total_lights": stats["total_lights"],
        "active_cells": stats["active_cells"],
        "save_s": t_save,
        "load_s": t_load,
        "save_mb": save_mb,
        "mem_world_mb": mem_after_gen / (1024 * 1024),
        "mem_index_mb": (mem_after_start - mem_after_gen) / (1024 * 1024),
        "cross_changed": changed,
    }


def run_benchmark(sizes):
    print("Big World streaming benchmark")
    print("=" * 96)
    header = (f"{'brushes':>9} {'startup':>8} {'still/frame':>12} {'per-cross':>10} "
              f"{'active/total brushes':>22} {'save':>7} {'load':>7} {'idx-mem':>8}")
    print(header)
    print("-" * 96)
    results = []
    for n in sizes:
        r = benchmark_size(n)
        results.append(r)
        print(f"{r['brushes']:>9,} "
              f"{r['startup_s']*1e3:>7.0f}m "
              f"{r['frame_still_us']:>10.2f}us "
              f"{r['per_cross_ms']:>8.2f}ms "
              f"{r['active_brushes']:>9,}/{r['total_brushes']:<11,} "
              f"{r['save_s']*1e3:>5.0f}m "
              f"{r['load_s']*1e3:>5.0f}m "
              f"{r['mem_index_mb']:>6.1f}M")
    print("-" * 96)
    print("Reading the table:")
    print("  • startup      one-off: index every brush into cells + park the world.")
    print("  • still/frame  per-frame cost with the player stationary — a cell-compare")
    print("                 early-out, so it stays flat (microseconds) at every size.")
    print("  • per-cross    cost when the player crosses a cell boundary: touches only")
    print("                 the strip of cells that changed — bounded by the radius,")
    print("                 NOT by total world size.")
    print("  • active/total the payoff: active brushes stay ~constant while the world")
    print("                 grows to 500k. The renderer/physics/AI only see 'active'.")
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Big World generator + benchmark")
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("benchmark", help="Run the streaming benchmark")
    b.add_argument("--sizes", default="10000,50000,100000,250000,500000",
                   help="Comma-separated brush counts")

    g = sub.add_parser("generate", help="Write a synthetic Big World map JSON")
    g.add_argument("--brushes", type=int, default=100000)
    g.add_argument("--out", default="maps/bigworld_generated.json")
    g.add_argument("--activation-radius", type=float, default=2048.0)

    args = ap.parse_args(argv)
    if args.cmd == "generate":
        write_map(args.out, args.brushes, args.activation_radius)
    else:  # default to benchmark
        sizes = [int(s) for s in getattr(args, "sizes",
                 "10000,50000,100000,250000,500000").split(",") if s.strip()]
        run_benchmark(sizes)


if __name__ == "__main__":
    main()
