"""Sustained-editing benchmark for the angled-brush renderer.

Fio's editor is its runtime, so the renderer has to stay cheap while the world
changes, not only when it is still. This drives a deterministic script of
edits -- one per frame, interleaved with still frames -- through a production
``RenderTable`` and ``ForwardRenderer`` from a fixed camera, and records for
every frame:

* CPU ms (this thread), split into the table refresh (``sync``, which the
  logic thread does in the app) and the renderer's own (``render``); wall ms;
  and GPU ms
  measured by a ``GL_TIME_ELAPSED`` query around the frame (OpenGL 3.3 core),
  since CPU time alone does not show a stall the GPU takes;
* bytes uploaded to the shared convex store by kind (geometry, transforms,
  indices), mesh builds, and the store's allocation state (pages, capacity,
  live vertices, transform slots);

and reports, per operation, median / p95 / p99 / max of each timing and the
mean of each count. Operations: ``still``, ``reshape`` (clip one brush),
``retexture`` (one face), ``drag`` (an editor move: the planes move), ``mover``
(transform only, as a mover moves), ``create``, ``delete``, ``hide``,
``unhide``, ``undo`` (the whole world restored as new dicts, as
``EditorState.restore_state`` does).

Run it at several world sizes to see whether an edit costs what it touches or
what the world holds::

    xvfb-run -a python tools/bench_geo_edit.py --grid 12 24 48 \\
        --batching off elements multidraw --json out.json [--tree other/checkout]

``--map maps/_SHOWCASE.json`` benchmarks a real map instead of the generated
field. ``--tree`` runs another checkout (the base revision has no shared store:
its upload and allocation columns read 0).
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

OPS = ("still", "reshape", "retexture", "drag", "mover", "create", "delete",
       "hide", "unhide", "undo")


def _percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(math.ceil(fraction * len(ordered))) - 1)]


def _summary(values):
    return {"median": round(statistics.median(values), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "p99": round(_percentile(values, 0.99), 3),
            "max": round(max(values), 3)}


def _cut(bg, brush, rng):
    import numpy as np
    n = np.array([rng.uniform(-1, 1), rng.uniform(0.5, 1.4), rng.uniform(-1, 1)])
    n /= np.linalg.norm(n)
    point = np.asarray(brush["pos"], dtype=float) + n * (0.15 * min(brush["size"]))
    return bg.clip_brush(brush, n.tolist(), float(n @ point))


def _load_map(path):
    with open(path) as handle:
        return json.load(handle)["brushes"]


def _camera(brushes, width, height):
    import glm
    import numpy as np
    pos = np.array([b.get("pos", (0, 0, 0)) for b in brushes], dtype=float)
    centre = pos.mean(axis=0)
    extent = float(np.ptp(pos[:, [0, 2]], axis=0).max()) or 512.0
    eye = glm.vec3(centre[0], centre[1] + 0.45 * extent, centre[2] + 0.9 * extent)
    projection = glm.perspective(glm.radians(70.0), width / height, 1.0, 40000.0)
    view = glm.lookAt(eye, glm.vec3(*centre), glm.vec3(0, 1, 0))
    return projection, view, eye


class Run:
    def __init__(self, renderer, brushes, batching, width, height, seed):
        import numpy as np
        from engine.render_table import RenderTable
        self.np = np
        self.renderer = renderer
        if hasattr(renderer, "geo_batching"):
            renderer.geo_batching = batching
        self.brushes = brushes
        self.table = RenderTable()
        self.epoch = 1
        self.rng = random.Random(seed)
        self.camera = _camera(brushes, width, height)
        self.hidden = []
        self.builds = 0
        build = renderer._build_geo_mesh

        def counted(*args, **kwargs):
            self.builds += 1
            return build(*args, **kwargs)

        renderer._build_geo_mesh = counted
        import OpenGL.GL as gl
        self.gl = gl
        self.query = gl.glGenQueries(1)[0]

    def _config(self):
        import numpy as np
        from engine.constants import RENDER_MODE_LIT
        from engine.entity_table import EntityTable
        if not hasattr(self, "_etable"):
            self._etable = EntityTable()
            self._hidden = self._etable.begin_frame([], epoch=1)
        _mask, slots = self.table.shown()
        return {"render_mode": RENDER_MODE_LIT, "brush_display_mode": "Textured",
                "play_mode": False, "grid_visible": False,
                "camera_distance_cull": False, "shadows_enabled": False,
                "render_table": self.table, "all_brush_slots": slots.astype(np.int32),
                "entity_table": self._etable,
                "visible_thing_slots": np.arange(0, dtype=np.int32),
                "thing_hidden": self._hidden, "all_brushes": self.brushes,
                "all_things": []}

    def _store(self):
        store = getattr(self.renderer, "_geo_store", None)
        return store() if store is not None else None

    def _uploads(self):
        store = self._store()
        return dict(store.uploads) if store is not None else {
            "geometry": 0, "transforms": 0, "indices": 0}

    def _allocation(self):
        store = self._store()
        if store is None:
            return {"pages": 0, "capacity": 0, "live": 0, "slots": 0}
        live = [p for p in store.pages if p is not None]
        return {"pages": len(live), "capacity": sum(p.capacity for p in live),
                "live": sum(p.used for p in live), "slots": store._next_slot}

    def edit(self, op):
        """Apply *op* to the world; returns the objects it dirtied, or None
        for a global invalidation."""
        from engine import brush_geometry as bg
        from engine.change_journal import moved
        from engine.spatial import set_authored_flag
        rng = self.rng
        angled = [b for b in self.brushes if b.get("geometry") and not b.get("hidden")]
        if op == "still":
            return ()
        if op == "reshape":
            brush = rng.choice(angled)
            _cut(bg, brush, rng)
            return (brush,)
        if op == "retexture":
            brush = rng.choice(angled)
            plane = rng.choice(brush["geometry"]["planes"])
            plane["texture"] = rng.choice(("Dev/128.jpg", "Dev/256.jpg",
                                           "Dev/512.jpg", "concrete02.png"))
            bg.invalidate_geometry_cache(brush)
            return (brush,)
        if op == "drag":
            brush = rng.choice(angled)
            bg.translate_brush(brush, (rng.uniform(-16, 16), 0.0, rng.uniform(-16, 16)))
            return (brush,)
        if op == "mover":
            brush = rng.choice(angled)
            brush["pos"] = [brush["pos"][0] + rng.uniform(-8, 8), brush["pos"][1],
                            brush["pos"][2] + rng.uniform(-8, 8)]
            moved(brush)
            return ()
        if op == "create":
            source = rng.choice(angled)
            brush = copy.deepcopy({k: v for k, v in source.items()
                                   if k not in bg.GEO_RUNTIME_KEYS})
            brush["id"] = "bench-new-%d" % rng.randrange(1 << 30)
            bg.translate_brush(brush, (rng.uniform(-64, 64), 0.0, rng.uniform(-64, 64)))
            _cut(bg, brush, rng)
            self.brushes.append(brush)
            return (brush,)
        if op == "delete":
            brush = rng.choice(angled)
            self.brushes.remove(brush)
            return (brush,)
        if op == "hide":
            brush = rng.choice(angled)
            set_authored_flag(brush, "hidden", True)
            self.hidden.append(brush)
            return ()
        if op == "unhide":
            if self.hidden:
                set_authored_flag(self.hidden.pop(), "hidden", False)
            return ()
        if op == "undo":
            # EditorState.restore_state: every brush a new dict, rebuilt from
            # its saved form (runtime keys gone), the world invalidated.
            restored = []
            for brush in self.brushes:
                saved = {k: v for k, v in brush.items() if k not in bg.GEO_RUNTIME_KEYS}
                restored.append(json.loads(json.dumps(saved, default=list)))
            self.brushes[:] = restored
            self.hidden = []
            return None
        raise ValueError(op)

    def frame(self, op):
        gl = self.gl
        uploads = self._uploads()
        builds = self.builds
        dirty = self.edit(op)
        c0, t0 = time.thread_time(), time.perf_counter()
        if dirty is None:
            self.epoch += 1
            self.table.begin_frame(self.brushes, epoch=self.epoch)
        elif dirty:
            self.epoch += 1
            self.table.begin_frame(self.brushes, epoch=self.epoch,
                                   dirty_objects={id(b) for b in dirty})
        else:
            self.table.begin_frame(self.brushes, epoch=self.epoch)
        cs = time.thread_time()
        config = self._config()
        gl.glBeginQuery(gl.GL_TIME_ELAPSED, self.query)
        self.renderer.render_scene(*self.camera, None, config,
                                   brush_slots=config["all_brush_slots"])
        gl.glEndQuery(gl.GL_TIME_ELAPSED)
        c1 = time.thread_time()
        gl.glFinish()
        t1 = time.perf_counter()
        import ctypes
        from OpenGL.raw.GL.VERSION.GL_3_3 import glGetQueryObjectui64v
        elapsed = ctypes.c_uint64(0)
        glGetQueryObjectui64v(self.query, gl.GL_QUERY_RESULT, ctypes.byref(elapsed))
        gpu_ns = int(elapsed.value)
        after = self._uploads()
        return {"op": op, "cpu": (c1 - c0) * 1000.0, "wall": (t1 - t0) * 1000.0,
                "sync": (cs - c0) * 1000.0, "render": (c1 - cs) * 1000.0,
                "gpu": gpu_ns / 1e6, "builds": self.builds - builds,
                **{"up_" + k: after[k] - uploads[k] for k in after},
                **self._allocation()}


def measure(renderer, brushes, batching, width, height, frames, seed=11):
    run = Run(renderer, brushes, batching, width, height, seed)
    run.table.begin_frame(run.brushes, epoch=run.epoch)
    for _ in range(10):
        run.frame("still")
    rng = random.Random(seed + 1)
    script = []
    edits = [op for op in OPS if op != "still"]
    for index in range(frames):
        # Every other frame an edit, otherwise still; undo is rarer.
        if index % 2:
            op = rng.choice(edits)
            if op == "undo" and rng.random() < 0.7:
                op = "reshape"
            script.append(op)
        else:
            script.append("still")
    records = [run.frame(op) for op in script]
    by_op = {}
    for op in OPS:
        rows = [r for r in records if r["op"] == op]
        if not rows:
            continue
        by_op[op] = {
            "frames": len(rows),
            "cpu_ms": _summary([r["cpu"] for r in rows]),
            "sync_ms": _summary([r["sync"] for r in rows]),
            "render_ms": _summary([r["render"] for r in rows]),
            "wall_ms": _summary([r["wall"] for r in rows]),
            "gpu_ms": _summary([r["gpu"] for r in rows]),
            "builds_mean": round(statistics.mean(r["builds"] for r in rows), 2),
            "upload_bytes_mean": {k: round(statistics.mean(r["up_" + k] for r in rows))
                                  for k in ("geometry", "transforms", "indices")},
            "upload_bytes_max": {k: max(r["up_" + k] for r in rows)
                                 for k in ("geometry", "transforms", "indices")},
        }
    everything = {
        "cpu_ms": _summary([r["cpu"] for r in records]),
        "render_ms": _summary([r["render"] for r in records]),
        "wall_ms": _summary([r["wall"] for r in records]),
        "gpu_ms": _summary([r["gpu"] for r in records]),
    }
    final = records[-1]
    allocation = {k: final[k] for k in ("pages", "capacity", "live", "slots")}
    allocation["capacity_max"] = max(r["capacity"] for r in records)
    renderer._build_geo_mesh = renderer._build_geo_mesh.__closure__[0].cell_contents \
        if renderer._build_geo_mesh.__closure__ else renderer._build_geo_mesh
    return {"all_frames": everything, "by_op": by_op, "allocation": allocation,
            "brushes": len(run.brushes)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grid", type=int, nargs="*", default=[24])
    parser.add_argument("--map", help="benchmark a map's brushes instead")
    parser.add_argument("--batching", nargs="*", default=["off", "elements"])
    parser.add_argument("--frames", type=int, default=400)
    parser.add_argument("--size", default="960x540")
    parser.add_argument("--json")
    parser.add_argument("--tree", default=os.path.dirname(HERE))
    args = parser.parse_args(argv)
    if os.environ.get("FIO_GL_DEBUG") != "1":
        os.environ.setdefault("PYOPENGL_ERROR_CHECKING", "0")
    tree = os.path.abspath(args.tree)
    out = os.path.abspath(args.json) if args.json else None
    map_path = os.path.abspath(args.map) if args.map else None
    sys.path.insert(0, HERE)
    import bench_geo_passes as passes
    os.chdir(tree)
    sys.path.insert(0, tree)
    width, height = (int(v) for v in args.size.split("x"))
    _app, _surface, _ctx = passes._context(width, height)
    from engine.renderer.forward import ForwardRenderer
    results = {"tree": tree, "size": [width, height], "runs": []}
    worlds = ([("map:" + os.path.basename(map_path), lambda: _load_map(map_path))]
              if map_path else
              [("grid%d" % g, (lambda g=g: passes.angled_world(g))) for g in args.grid])
    for label, make in worlds:
        for batching in args.batching:
            renderer = ForwardRenderer(None)
            renderer.shadows_enabled = False
            renderer.set_grid(4096, 64)
            renderer.set_sprite_textures({})
            brushes = make()
            result = measure(renderer, brushes, batching, width, height, args.frames)
            result.update(world=label, batching=batching)
            results["runs"].append(result)
            renderer.cleanup()
            line = result["by_op"]
            print("%-16s %-9s brushes %5d | all frames cpu %s gpu %s | still cpu p50 %.2f "
                  "p99 %.2f | reshape cpu p50 %.2f max %.2f geom %dB idx %dB | undo cpu max %s "
                  "| pages %d cap %d (max %d) live %d slots %d" % (
                      label, batching, result["brushes"], result["all_frames"]["cpu_ms"],
                      result["all_frames"]["gpu_ms"], line["still"]["cpu_ms"]["median"],
                      line["still"]["cpu_ms"]["p99"],
                      line.get("reshape", {}).get("cpu_ms", {}).get("median", 0),
                      line.get("reshape", {}).get("cpu_ms", {}).get("max", 0),
                      line.get("reshape", {}).get("upload_bytes_mean", {}).get("geometry", 0),
                      line.get("reshape", {}).get("upload_bytes_mean", {}).get("indices", 0),
                      line.get("undo", {}).get("cpu_ms", {}).get("max", "-"),
                      result["allocation"]["pages"], result["allocation"]["capacity"],
                      result["allocation"]["capacity_max"], result["allocation"]["live"],
                      result["allocation"]["slots"]), flush=True)
    if out:
        with open(out, "w") as handle:
            json.dump(results, handle, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
