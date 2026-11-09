"""Per-frame GPU uploads and render CPU, attributed (renderer M3 groundwork).

    xvfb-run -a python tools/profile_frame_uploads.py MAP [--tree PATH] [--movers K]

Renders a map's brushes still, with K box brushes moving (runtime transforms
published into the render table, as the mover table does) and with one brush
edited per frame, and reports for each: render_scene p50, bytes and calls
uploaded per frame by every buffer upload site (attributed to the renderer
function that made it), and a function-level profile of render_scene.
Measurement only. See docs/renderer-m3-profiling.md.
"""
import argparse
import collections
import cProfile
import io
import json
import os
import pstats
import statistics
import sys
import time
import traceback

ap = argparse.ArgumentParser()
ap.add_argument('map')
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
ap.add_argument('--movers', type=int, default=64)
ap.add_argument('--frames', type=int, default=60)
ap.add_argument('--size', default='640x360')
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path.insert(0, tree)
sys.path.insert(0, os.path.join(tree, 'tools'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np                                    # noqa: E402
import OpenGL.GL as gl                                # noqa: E402
import bench_geo_passes as passes                     # noqa: E402
from bench_geo_edit import _camera                    # noqa: E402
from bench_brush_model import _raw                    # noqa: E402
from engine import brush_placement as bp              # noqa: E402
from engine.constants import RENDER_MODE_LIT          # noqa: E402
from engine.entity_table import EntityTable           # noqa: E402
from engine.render_table import RenderTable           # noqa: E402
from engine.renderer.forward import ForwardRenderer   # noqa: E402

width, height = (int(v) for v in args.size.split('x'))
keep = passes._context(width, height)

# -- upload accounting --------------------------------------------------------
uploads = collections.Counter()      # (site, call) -> bytes
calls = collections.Counter()


def _site():
    for frame in reversed(traceback.extract_stack(limit=8)[:-2]):
        if '/engine/renderer/' in frame.filename:
            return '%s:%s' % (os.path.basename(frame.filename), frame.name)
    return '?'


def _wrap(name):
    real = getattr(gl, name)

    def wrapped(*a, **k):
        size = 0
        for v in a:
            if hasattr(v, 'nbytes'):
                size = int(v.nbytes)
        if name in ('glBufferData',) and len(a) > 1 and isinstance(a[1], int):
            size = int(a[1])
        if name == 'glBufferSubData' and len(a) > 2 and isinstance(a[2], int):
            size = int(a[2])
        key = (_site(), name)
        uploads[key] += size
        calls[key] += 1
        return real(*a, **k)
    setattr(gl, name, wrapped)


for name in ('glBufferData', 'glBufferSubData', 'glTexSubImage2D'):
    _wrap(name)
# The renderer modules imported gl as a module object: patching the attribute
# reaches every `gl.glBufferSubData(...)` call.

renderer = ForwardRenderer(None)
renderer.shadows_enabled = False
renderer.set_grid(4096, 64)
renderer.set_sprite_textures({})
brushes = json.loads(json.dumps(_raw(tree, args.map)['brushes']))
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

source = bp.RuntimeOverrides()
bp.register_runtime(source)
boxes = [i for i, b in enumerate(brushes) if not b.get('geometry')][:args.movers]
for i in boxes:
    brushes[i]['is_mover'] = True
homes = {i: list(brushes[i]['pos']) for i in boxes}


def frame():
    renderer.render_stats.reset()
    t0 = time.perf_counter()
    renderer.render_scene(*camera, None, config, brush_slots=config["all_brush_slots"])
    spent = (time.perf_counter() - t0) * 1e3
    gl.glFinish()
    return spent


def scenario(label, step, frames):
    for k in range(10):
        step(k)
        frame()
    uploads.clear()
    calls.clear()
    times = []
    pr = cProfile.Profile()
    for k in range(frames):
        step(10 + k)
        pr.enable()
        times.append(frame())
        pr.disable()
    total = sum(uploads.values())
    print('\n== %s: %d frames, render_scene p50 %.2f ms, uploads %.1f KB/frame, %.1f calls/frame'
          % (label, frames, statistics.median(times), total / frames / 1024.0,
             sum(calls.values()) / frames))
    for key, size in uploads.most_common(8):
        print('   %-48s %-16s %9.1f KB/frame %6.1f calls/frame'
              % (key[0], key[1], size / frames / 1024.0, calls[key] / frames))
    buf = io.StringIO()
    pstats.Stats(pr, stream=buf).sort_stats('tottime').print_stats(10)
    for line in buf.getvalue().splitlines():
        if '/' in line or '{' in line:
            print('   ' + line.replace(tree + '/', '')[:132])


def still(k):
    return None


def moving(k):
    for i in boxes:
        h = homes[i]
        source.set(brushes[i], centre=(h[0], h[1] + 8.0 * ((k + i) % 7), h[2]))
    table.refresh_transforms(brushes, np.asarray(boxes, dtype=np.intp))


def editing(k):
    if not boxes:
        return
    i = boxes[k % len(boxes)]
    p = brushes[i]['pos']
    brushes[i]['pos'] = [p[0] + (4.0 if k % 2 else -4.0), p[1], p[2]]
    table.begin_frame(brushes, epoch=2 + k, dirty_objects={id(brushes[i])})


print('%s  %s  brushes %d (convex %d), %d box movers, %dx%d'
      % (os.path.basename(tree), args.map, len(brushes),
         sum(1 for b in brushes if b.get('geometry')), len(boxes), width, height))
scenario('still', still, args.frames)
scenario('moving (%d box movers)' % len(boxes), moving, args.frames)
source.clear()
scenario('editing (one brush moved per frame)', editing, args.frames)
renderer.cleanup()
del keep
