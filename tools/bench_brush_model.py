"""Baselines the unified brush model must not regress, AABBs first.

Stage 0 of ``docs/brush-model-proposal.md``. Every later stage is measured
against these numbers, on the same machine, base against branch:

* **memory** -- Python heap per box dict and per convex dict as parsed; what
  ``EditorState``'s loader adds per brush (its shallow copy and keys);
  RenderTable column bytes and retained bytes per row;
* **table sync** -- a full RenderTable sync of every brush (the 40 ms
  BigWorld budget), and the one-row refresh an edit costs;
* **edit latency** -- each editor operation through ``EditorState`` as the
  editor performs it: the undo checkpoint, the change, the journal, then the
  table refresh the logic thread makes (``render_dirty_since`` +
  ``begin_frame``). Move, resize, retexture, UV edit, create, delete, undo,
  redo; median / p95 / p99 / max;
* **retained memory** -- bytes per undo checkpoint (a JSON snapshot of the
  whole world) and what the undo/redo history holds after the burst;
* **steady rendering** (``--render``, needs a GL 3.3 context: run under
  ``xvfb-run -a``) -- still-frame render CPU time, median / p95 / p99 / max,
  and draw calls.

    QT_QPA_PLATFORM=offscreen python tools/bench_brush_model.py --json base.json
    xvfb-run -a python tools/bench_brush_model.py --render --json base.json
    python tools/bench_brush_model.py --tree <checkout> ...   # another revision

Timings are wall-clock on whatever machine runs this; compare a branch only
with a base measured on the same machine.
"""

import argparse
import copy
import gc
import json
import os
import random
import statistics
import sys
import time
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
MAPS = ('BigWorld_streaming_test.json', '_SHOWCASE.json')
OPS = ('move', 'resize', 'retexture', 'uv_edit', 'create', 'delete', 'undo', 'redo')


def _percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _summary(values):
    return {"median": round(statistics.median(values), 3),
            "p95": round(_percentile(values, 0.95), 3),
            "p99": round(_percentile(values, 0.99), 3),
            "max": round(max(values), 3), "n": len(values)}


def _heap(make):
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        made = make()
        gc.collect()
        used = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    return made, used


def _raw(tree, name):
    with open(os.path.join(tree, 'maps', name)) as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Memory and table
# ---------------------------------------------------------------------------

def memory_and_table(tree, name, repeats):
    import numpy as np
    from engine import render_table as rt
    from engine.render_table import RenderTable
    data = _raw(tree, name)
    brushes = data['brushes']
    out = {"brushes": len(brushes)}
    for kind, subset in (("box", [b for b in brushes if not b.get('geometry')]),
                         ("convex", [b for b in brushes if b.get('geometry')])):
        if subset:
            copies, used = _heap(lambda s=subset: json.loads(json.dumps(s)))
            out["heap_per_%s_B" % kind] = round(used / len(copies), 1)
            out["%s_count" % kind] = len(copies)
    out["row_column_B"] = sum(np.dtype(d).itemsize * int(np.prod(s or (1,)))
                              for _n, s, d, _f in rt._COLUMNS)
    copies = json.loads(json.dumps(brushes))

    def sync():
        table = RenderTable()
        table.sync(copies, 1)
        return table

    table, used = _heap(sync)
    out["row_retained_B"] = round(used / len(copies), 1)
    times = []
    for _ in range(repeats):
        fresh = json.loads(json.dumps(brushes))     # cold: no cached convex shapes
        t0 = time.perf_counter()
        RenderTable().sync(fresh, 1)
        times.append((time.perf_counter() - t0) * 1000.0)
    out["full_sync_cold_ms"] = _summary(times)
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        RenderTable().sync(copies, 1)
        times.append((time.perf_counter() - t0) * 1000.0)
    out["full_sync_warm_ms"] = _summary(times)
    rng = random.Random(3)
    one = []
    for epoch in range(2, 2 + 200):
        brush = copies[rng.randrange(len(copies))]
        brush['pos'] = [brush['pos'][0] + 1.0] + list(brush['pos'][1:])
        t0 = time.perf_counter()
        table.sync(copies, epoch, dirty_objects={id(brush)})
        one.append((time.perf_counter() - t0) * 1000.0)
    out["one_row_refresh_ms"] = _summary(one)
    return out


# ---------------------------------------------------------------------------
# Editing through EditorState
# ---------------------------------------------------------------------------

class Session:
    """An EditorState with a map loaded and the logic thread's table refresh."""

    def __init__(self, tree, name, seed):
        from editor.editor_state import EditorState
        from engine.render_table import RenderTable
        self.state = EditorState()
        data = _raw(tree, name)
        (_none, self.load_heap) = _heap(
            lambda: self.state.load_from_data(data, stream_editor=False))
        self.table = RenderTable()
        self.rng = random.Random(seed)
        self.serial = 0
        self.refresh()

    def refresh(self):
        """What ``logic_render`` does each frame for the brush table."""
        state = self.state
        snapshot_epoch, _ = state.render_dirty_snapshot()
        epoch, dirty = state.render_dirty_since(
            getattr(self.table, '_epoch', None), through_epoch=snapshot_epoch)
        self.table.begin_frame(state.brushes, epoch, dirty_objects=dirty)

    def _pick(self):
        brushes = [b for b in self.state.brushes if not b.get('geometry')]
        return self.rng.choice(brushes or self.state.brushes)

    def edit(self, op):
        """Apply *op* as the editor does; returns (checkpoint ms, change ms)."""
        from editor import face_texture as ft
        state = self.state
        if op in ('undo', 'redo'):
            t0 = time.perf_counter()
            done = state.undo() if op == 'undo' else state.redo()
            return 0.0, (time.perf_counter() - t0) * 1000.0, done
        brush = self._pick()
        state.selected_objects = [brush]
        t0 = time.perf_counter()
        state.save_state()
        t1 = time.perf_counter()
        if op == 'move':
            brush['pos'] = [brush['pos'][0] + 16.0] + list(brush['pos'][1:])
        elif op == 'resize':
            brush['size'] = [brush['size'][0] + 16.0] + list(brush['size'][1:])
        elif op == 'retexture':
            self.serial += 1
            ft.set_transform(brush, 'top',
                             texture=('Dev/128.jpg', 'Dev/256.jpg')[self.serial % 2])
        elif op == 'uv_edit':
            ft.set_transform(brush, 'top', shift=(self.rng.random(), 0.0))
        elif op == 'create':
            self.serial += 1
            new = copy.deepcopy({k: v for k, v in brush.items()
                                 if not k.startswith('_')})
            new['id'] = 'bench-new-%d' % self.serial
            new['pos'] = [new['pos'][0], new['pos'][1] + 256.0, new['pos'][2]]
            new['_io_connections'] = []
            state.brushes.append(new)
            brush = new
        elif op == 'delete':
            state.brushes.remove(brush)
            state.selected_objects = []
        state.mark_world_changed([brush])
        return (t1 - t0) * 1000.0, (time.perf_counter() - t1) * 1000.0, True


def editing(tree, name, rounds, seed=11):
    session = Session(tree, name, seed)
    state = session.state
    out = {"brushes": len(state.brushes),
           "load_heap_per_brush_B": round(session.load_heap / max(1, len(state.brushes)), 1)}
    rows = {op: {"checkpoint": [], "change": [], "table": [], "total": []} for op in OPS}
    sizes = []
    for _ in range(rounds):
        for op in OPS:
            checkpoint, change, done = session.edit(op)
            if not done:
                continue
            t0 = time.perf_counter()
            session.refresh()
            table = (time.perf_counter() - t0) * 1000.0
            row = rows[op]
            row["checkpoint"].append(checkpoint)
            row["change"].append(change)
            row["table"].append(table)
            row["total"].append(checkpoint + change + table)
        if state.undo_stack and isinstance(state.undo_stack[-1], str):
            sizes.append(len(state.undo_stack[-1]))
    out["ops_ms"] = {op: {k: _summary(v) for k, v in row.items() if v}
                     for op, row in rows.items() if row["total"]}
    out["undo_checkpoint_B"] = round(statistics.mean(sizes)) if sizes else 0
    out["undo_stack_entries"] = len(state.undo_stack)
    # What the history retains after the burst: every checkpoint is a JSON
    # string of the whole world (ASCII, so one byte a character).
    out["history_retained_B"] = sum(len(entry) for entry in
                                    list(state.undo_stack) + list(state.redo_stack)
                                    if isinstance(entry, str))
    return out


# ---------------------------------------------------------------------------
# Steady rendering
# ---------------------------------------------------------------------------

def rendering(tree, name, frames, size):
    sys.path.insert(0, HERE)
    import bench_geo_passes as passes
    from bench_geo_edit import _camera
    import numpy as np
    from engine.constants import RENDER_MODE_LIT
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    from engine.renderer.forward import ForwardRenderer
    width, height = size
    keep = passes._context(width, height)      # the context lives as long as this
    renderer = ForwardRenderer(None)
    renderer.shadows_enabled = False
    renderer.set_grid(4096, 64)
    renderer.set_sprite_textures({})
    brushes = json.loads(json.dumps(_raw(tree, name)['brushes']))
    table = RenderTable()
    table.begin_frame(brushes, epoch=1)
    etable = EntityTable()
    hidden = etable.begin_frame([], epoch=1)
    _mask, slots = table.shown()
    config = {"render_mode": RENDER_MODE_LIT, "brush_display_mode": "Textured",
              "play_mode": False, "grid_visible": False,
              "camera_distance_cull": False, "shadows_enabled": False,
              "render_table": table, "all_brush_slots": slots.astype(np.int32),
              "entity_table": etable,
              "visible_thing_slots": np.arange(0, dtype=np.int32),
              "thing_hidden": hidden, "all_brushes": brushes, "all_things": []}
    camera = _camera(brushes, width, height)
    import OpenGL.GL as gl
    times, draws = [], []
    for index in range(frames + 10):
        renderer.render_stats.reset()
        t0 = time.perf_counter()
        renderer.render_scene(*camera, None, config,
                              brush_slots=config["all_brush_slots"])
        elapsed = (time.perf_counter() - t0) * 1000.0
        gl.glFinish()
        if index >= 10:
            times.append(elapsed)
            draws.append(int(getattr(renderer.render_stats, 'draw_calls', 0)))
    renderer.cleanup()
    del keep
    return {"brushes": len(brushes), "render_cpu_ms": _summary(times),
            "draw_calls": statistics.median(draws)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--maps", nargs="*", default=list(MAPS))
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--rounds", type=int, default=40,
                        help="edit rounds (each round runs every operation once)")
    parser.add_argument("--render", action="store_true",
                        help="also measure steady rendering (needs GL 3.3)")
    parser.add_argument("--frames", type=int, default=200)
    parser.add_argument("--size", default="960x540")
    parser.add_argument("--no-edit", action="store_true")
    parser.add_argument("--json")
    parser.add_argument("--tree", default=os.path.dirname(HERE))
    args = parser.parse_args(argv)
    os.environ.setdefault("PYOPENGL_ERROR_CHECKING", "0")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen" if not args.render else
                          os.environ.get("QT_QPA_PLATFORM", "xcb"))
    tree = os.path.abspath(args.tree)
    out_path = os.path.abspath(args.json) if args.json else None
    os.chdir(tree)
    sys.path.insert(0, tree)
    size = tuple(int(v) for v in args.size.split("x"))
    results = {"tree": tree, "python": sys.version.split()[0], "maps": {}}
    for name in args.maps:
        entry = {"table": memory_and_table(tree, name, args.repeats)}
        if not args.no_edit:
            entry["edit"] = editing(tree, name, args.rounds)
        if args.render:
            entry["render"] = rendering(tree, name, args.frames, size)
        results["maps"][name] = entry
        table = entry["table"]
        print("%s: %d brushes | heap/box %s B heap/convex %s B | row %d B, retained %s B | "
              "full sync cold %.1f ms warm %.1f ms | one-row %.3f ms" % (
                  name, table["brushes"], table.get("heap_per_box_B", "-"),
                  table.get("heap_per_convex_B", "-"), table["row_column_B"],
                  table["row_retained_B"], table["full_sync_cold_ms"]["median"],
                  table["full_sync_warm_ms"]["median"],
                  table["one_row_refresh_ms"]["median"]), flush=True)
        if "edit" in entry:
            edit = entry["edit"]
            print("   loader adds %s B/brush | undo checkpoint %d B | history retains %.1f MB "
                  "(%d entries)" % (
                      edit["load_heap_per_brush_B"], edit["undo_checkpoint_B"],
                      edit["history_retained_B"] / 1e6, edit["undo_stack_entries"]))
            for op, row in edit["ops_ms"].items():
                total = row["total"]
                print("   %-9s total p50 %7.2f p95 %7.2f p99 %7.2f max %7.2f ms "
                      "(checkpoint %.2f, change %.2f, table %.2f)" % (
                          op, total["median"], total["p95"], total["p99"], total["max"],
                          row.get("checkpoint", {}).get("median", 0.0),
                          row["change"]["median"], row["table"]["median"]), flush=True)
        if "render" in entry:
            render = entry["render"]["render_cpu_ms"]
            print("   still frame p50 %.2f p95 %.2f p99 %.2f max %.2f ms, %s draws" % (
                render["median"], render["p95"], render["p99"], render["max"],
                entry["render"]["draw_calls"]), flush=True)
    if out_path:
        with open(out_path, "w") as handle:
            json.dump(results, handle, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
