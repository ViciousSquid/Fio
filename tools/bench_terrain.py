"""M5: what the terrain costs a frame, and where.

    xvfb-run -a python tools/bench_terrain.py [MAP] [--frames 40] [--tree PATH]
                                              [--json OUT] [--dump PNG]
                                              [--background]

Plays *MAP* (default ``BigWorld_streaming_test.json``) with the logic thread
stepped from here, walks the scripted path until the terrain around the
player has streamed in, then holds the camera still and times complete frames
(``paintGL`` followed by ``glFinish``) three ways:

* as drawn;
* with the terrain's draws under ``GL_RASTERIZER_DISCARD`` -- the vertex
  stage alone (what the vertex shader costs, without rasterising);
* with the terrain's draws skipped -- the frame without terrain.

It also reports what the terrain drew (chunks, their resolutions, triangles,
indices against distinct grid points), the LOD each chunk was
given and why (its distance against the protected zone and the LOD bands),
and the paint-thread time spent generating chunk heights while moving.
``--dump`` writes the held frame as a PNG, for parity between two trees.

Under software GL the vertex stage runs on the calling thread and the
numbers are llvmpipe's: compare trees on one host. ``settings.ini`` is
restored.
"""

import argparse
import collections
import json
import os
import sys
import time

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument('map', nargs='?', default='BigWorld_streaming_test.json')
ap.add_argument('--frames', type=int, default=40)
ap.add_argument('--walk', type=int, default=240, help='frames walked before holding still')
ap.add_argument('--json')
ap.add_argument('--dump')
ap.add_argument('--background', action='store_true',
                help='build height grids on the builder thread, as main.py runs')
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path[:0] = [tree, os.path.join(tree, 'tools')]
os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
settings = os.path.join(tree, 'settings.ini')
saved = open(settings, 'rb').read()

from PyQt5.QtCore import Qt                                   # noqa: E402
from PyQt5.QtWidgets import QApplication                      # noqa: E402
app = QApplication.instance() or QApplication([])
import numpy as np                                            # noqa: E402
import OpenGL.GL as gl                                        # noqa: E402
import bench_compare as bc                                    # noqa: E402
import engine.terrain as terrain_module                       # noqa: E402

Terrain = terrain_module.Terrain
try:                                    # M5.3 on: the GL side is the renderer's
    from engine.renderer.core.terrain_gl import TerrainGL as Drawer   # noqa: E402
except ImportError:                     # a tree before it
    Drawer = Terrain
if args.background:
    Terrain.BACKGROUND_BUILDS = True
mode = {'draw': 'normal'}
draws, builds = [], []
real_draw = Drawer.draw_heightfield_slots


def draw(self, u, slots, unit, *a, **k):
    if mode['draw'] == 'skip':
        return 0
    if mode['draw'] == 'vertex':
        gl.glEnable(gl.GL_RASTERIZER_DISCARD)
    started = time.perf_counter()
    try:
        triangles = real_draw(self, u, slots, unit, *a, **k)
        gl.glFinish()
    finally:
        if mode['draw'] == 'vertex':
            gl.glDisable(gl.GL_RASTERIZER_DISCARD)
    draws.append((mode['draw'], time.perf_counter() - started, np.array(slots), triangles))
    return triangles


Drawer.draw_heightfield_slots = draw
BUILDERS = [name for name in ('_upload_chunk', '_build_chunk', '_grid_heights',
                                '_build_grass', '_upload_grass_chunk')
            if hasattr(Terrain, name)]
if '_grid_heights' in BUILDERS and '_build_chunk' in BUILDERS:
    BUILDERS.remove('_build_chunk')     # the grid is the work; count it once
for name in BUILDERS:
    def timed(self, *a, _real=getattr(Terrain, name), **k):
        started = time.perf_counter()
        try:
            return _real(self, *a, **k)
        finally:
            import threading
            builds.append((threading.current_thread() is threading.main_thread(),
                           time.perf_counter() - started))
    setattr(Terrain, name, timed)


def med(values):
    values = sorted(values)
    return values[len(values) // 2] if values else 0.0


try:
    bench = bc.Bench(app)
    window, view = bench.window, bench.view
    logic = bench._halt_logic_thread()
    window.load_level_file(os.path.join(tree, 'maps', args.map))
    app.processEvents()
    window.enter_play_mode()
    app.processEvents()
    state = view.game_state
    import threading
    for thread in threading.enumerate():
        if thread.name == 'MonsterAIThread':
            thread.stop()
            thread.join(5.0)

    def frame(index, moving):
        if moving:
            keys, dx, dy, _ = bc._script(index, Qt.Key_W, Qt.Key_A)
        else:
            keys, dx, dy = set(), 0.0, 0.0
        state.set_keys(keys)
        state.set_mouse_delta(dx, dy)
        logic._step_frame(logic.TICK_DURATION)
        logic._publish_frame()
        state.try_swap()
        view.makeCurrent()
        started = time.perf_counter()
        view.paintGL()
        gl.glFinish()
        elapsed = time.perf_counter() - started
        view.doneCurrent()
        return elapsed

    walk = [frame(i, True) for i in range(args.walk)]
    walk_builds = list(builds)
    terrain = window.terrain
    # Hold still until nothing is left to build (a worker may still be busy).
    for i in range(600):
        before = len(builds)
        frame(0, False)
        time.sleep(0.002)
        table = terrain.table if terrain is not None else None
        if (i > 20 and len(builds) == before and table is not None
                and bool(np.all(table.built[table.live_slots()]))):
            break
    report = {'map': args.map, 'viewport': [view.width(), view.height()]}
    timings = {}
    for name in ('normal', 'vertex', 'skip', 'normal'):
        mode['draw'] = name
        del draws[:]
        frames = [frame(0, False) for _ in range(args.frames)]
        timings.setdefault(name, []).extend(frames)
        if name != 'skip':
            timings.setdefault(name + '_terrain', []).extend(d[1] for d in draws)
    mode['draw'] = 'normal'
    del draws[:]
    frame(0, False)
    if args.dump:
        view.makeCurrent()
        view.paintGL()
        view.doneCurrent()
        view.grabFramebuffer().save(args.dump)
    report['frame_ms'] = {k: round(med(v) * 1e3, 2) for k, v in timings.items()}
    report['frame_ms_min'] = {k: round(min(v) * 1e3, 2) for k, v in timings.items()}
    if terrain is not None and draws:
        table = terrain.table
        slots = draws[-1][2]
        res = table.grid_res[slots].astype(int)
        cam = view.camera.pos
        dist = np.sqrt(table.nearest_dist_sq(slots, float(cam.x), float(cam.z)))
        triangles = int(draws[-1][3])
        distinct = int(((res + 1) ** 2).sum())
        report['terrain'] = {
            'chunks_drawn': int(len(slots)), 'chunks_resident': int(len(table.live_slots())),
            'chunk_size': float(terrain.chunk_size),
            'resolutions': dict(collections.Counter(res.tolist())),
            'triangles': triangles, 'indices': triangles * 3,
            'distinct_grid_points': distinct,
            'chunk_distance': {'min': round(float(dist.min()), 1),
                               'median': round(float(np.median(dist)), 1),
                               'max': round(float(dist.max()), 1)},
            'streaming': bool(terrain.streaming),
            'stream_radius': float(terrain.stream_radius),
            'near_detail_radius': float(terrain._near_detail_radius()),
            'first_lod_distance': float(terrain.LOD_DISTANCES_SQ[0]) ** 0.5,
            'view_distance': float(getattr(view.view_distance, 'distance', 0.0)),
        }
    on_paint = [b[1] for b in walk_builds if b[0]]
    off_paint = [b[1] for b in walk_builds if not b[0]]
    report['walk'] = {
        'frames': args.walk,
        'frame_ms_median': round(med(walk) * 1e3, 2),
        'frame_ms_p95': round(sorted(walk)[int(len(walk) * .95)] * 1e3, 2),
        'frame_ms_max': round(max(walk) * 1e3, 2),
        'chunk_builds_on_paint_thread': len(on_paint),
        'paint_thread_build_ms_total': round(sum(on_paint) * 1e3, 1),
        'paint_thread_build_ms_max': round(max(on_paint, default=0.0) * 1e3, 2),
        'chunk_builds_off_paint_thread': len(off_paint),
    }
    text = json.dumps(report, indent=1)
    print(text)
    if args.json:
        with open(args.json, 'w') as f:
            f.write(text)
finally:
    with open(settings, 'wb') as f:
        f.write(saved)
os._exit(0)
