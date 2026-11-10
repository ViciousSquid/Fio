"""Per-frame GPU instance uploads, GL calls and render CPU (renderer M3).

    xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 \\
        python tools/bench_instance_uploads.py MAP [--tree PATH] [--json OUT]

Renders a map's brushes through ``ForwardRenderer.render_scene`` (lit mode,
textured display) from the dense render table, the way the editor and the
game do, in these scenarios:

* ``still``: nothing changes;
* ``camera``: the camera, nearer the map's centre, turns a little every
  frame and the visible set is frustum-culled as the logic thread culls it
  (``LogicRender.aabb_in_frustum_bounds``), so what is drawn changes;
* ``one_mover``: one box brush moves every frame (runtime transform
  published into the table, as the mover table does);
* ``movers64`` / ``movers512``: 64 / 512 box brushes move every frame;
* ``selection``: the selected brush alternates between two every frame;
* ``editing``: 32 brushes are edited every frame (journalled authored moves
  re-read by the table: high-churn editing);
* ``alternating``: two render tables of the same brushes take turns, as the
  double-buffered render state does, with 64 movers moving.

For each: ``render_scene`` p50 and minimum (ms), the bytes and calls a frame
uploads to instance buffers (any buffer upload made from
``engine/renderer/forward/instancing.py``) and to every buffer, the GL calls
a frame (the common entry points, wrapped), Python calls a frame (cProfile),
and the memory the frames allocate and keep (tracemalloc).

Run trees interleaved, pinned to one core (``taskset -c 3``), and compare.
"""
import argparse
import collections
import cProfile
import json
import math
import os
import pstats
import statistics
import sys
import time
import tracemalloc
import traceback

ap = argparse.ArgumentParser()
ap.add_argument('map')
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
ap.add_argument('--frames', type=int, default=60)
ap.add_argument('--size', default='640x360')
ap.add_argument('--only', default='')
ap.add_argument('--json')
ap.add_argument('--profile', default='',
                help='print the functions that took most time in this scenario')
ap.add_argument('--logic', action='store_true',
                help='also run the logic thread\'s per-frame publication before '
                     'each frame -- RenderTable.begin_frame and the frustum cull '
                     '(LogicRender.cull_brushes) -- timed apart as logic_ms')
ap.add_argument('--scale', type=int, default=1,
                help='tile the map this many times (a square of copies) to see '
                     'how change detection grows with the world (M4)')
ap.add_argument('--no-raster', action='store_true',
                help='make draw calls no-ops: render_scene then times the CPU '
                     'side alone (Python, GL calls, uploads), which is what the '
                     'target pays on its CPU; llvmpipe rasterises on the CPU')
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path.insert(0, tree)
sys.path.insert(0, os.path.join(tree, 'tools'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
# As main.py runs the game: PyOpenGL's error checking off (a debugging aid
# that adds a glGetError to every GL call timed here).
os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')

import glm                                            # noqa: E402
import numpy as np                                    # noqa: E402
import OpenGL.GL as gl                                # noqa: E402
import bench_geo_passes as passes                     # noqa: E402
from bench_brush_model import _raw                    # noqa: E402
from engine import brush_placement as bp              # noqa: E402
from engine.constants import RENDER_MODE_LIT          # noqa: E402
from engine.entity_table import EntityTable           # noqa: E402
from engine.render_table import RenderTable           # noqa: E402
try:
    from engine import change_detection as _change_detection   # noqa: E402
    if hasattr(_change_detection, 'timing'):
        _change_detection.timing(True)      # this tool reports the time
except ImportError:                     # a tree before M4
    _change_detection = None
from engine.renderer.forward import ForwardRenderer   # noqa: E402

width, height = (int(v) for v in args.size.split('x'))
keep = passes._context(width, height)

# -- GL accounting -------------------------------------------------------------
UPLOADS = ('glBufferData', 'glBufferSubData')
COUNTED = UPLOADS + (
    'glDrawArrays', 'glDrawArraysInstanced', 'glDrawElements',
    'glDrawElementsInstanced', 'glMultiDrawElements', 'glBindVertexArray',
    'glBindBuffer', 'glBindTexture', 'glUseProgram', 'glUniformMatrix4fv',
    'glUniformMatrix3fv', 'glUniform1f', 'glUniform1i', 'glUniform3f',
    'glUniform4f', 'glUniform3fv', 'glTexBuffer', 'glActiveTexture',
    'glPolygonMode', 'glEnable', 'glDisable', 'glVertexAttribPointer',
    'glVertexAttribIPointer', 'glEnableVertexAttribArray')
state = {'on': False, 'calls': collections.Counter(), 'bytes': collections.Counter()}


def _instance_site():
    for frame in reversed(traceback.extract_stack(limit=8)[:-2]):
        if '/engine/renderer/' in frame.filename:
            return frame.filename.endswith('instancing.py')
    return False


_REAL = {}


def _wrap(module, name):
    real = getattr(module, name, None)
    if real is None:
        return
    _REAL[name] = real

    def wrapped(*a, **k):
        if state['on']:
            state['calls'][name] += 1
            if name in UPLOADS:
                size = 0
                for v in a:
                    if hasattr(v, 'nbytes'):
                        size = int(v.nbytes)
                if name == 'glBufferData' and len(a) > 1 and isinstance(a[1], int):
                    size = int(a[1])
                if name == 'glBufferSubData' and len(a) > 2 and isinstance(a[2], int):
                    size = int(a[2])
                state['bytes']['all'] += size
                state['calls']['upload'] += 1
                if _instance_site():
                    state['bytes']['instance'] += size
                    state['calls']['instance_upload'] += 1
        return real(*a, **k)
    _WRAPPED[name] = wrapped


_WRAPPED = {}
if args.no_raster:
    def _no_draw(*a, **k):
        return None
    for _name in ('glDrawArrays', 'glDrawArraysInstanced', 'glDrawElements',
                  'glDrawElementsInstanced', 'glMultiDrawElements'):
        if hasattr(gl, _name):
            setattr(gl, _name, _no_draw)
for _name in COUNTED:
    _wrap(gl, _name)
# The instance attribute re-pointing goes through the raw entry point.
from engine.renderer.forward import instancing as _instancing   # noqa: E402
_real_raw = _instancing._raw_vertex_attrib_pointer


def _raw_counted(*a):
    if state['on']:
        state['calls']['glVertexAttribPointer'] += 1
    return _real_raw(*a)


def _counting(on):
    """Install the counting wrappers (on) or the real entry points: the
    timed frames run without them."""
    for name, wrapped in _WRAPPED.items():
        setattr(gl, name, wrapped if on else _REAL[name])
    _instancing._raw_vertex_attrib_pointer = _raw_counted if on else _real_raw
    state['on'] = on

# -- the scene -----------------------------------------------------------------
renderer = ForwardRenderer(None)
renderer.shadows_enabled = False
renderer.set_grid(4096, 64)
renderer.set_sprite_textures({})
brushes = json.loads(json.dumps(_raw(tree, args.map)['brushes']))
if args.scale > 1:
    # Copies of the map side by side, each brush with its own id: a bigger
    # world around the same view (the camera stays on the first copy).
    _xz = np.array([b.get('pos', (0, 0, 0)) for b in brushes], dtype=float)[:, [0, 2]]
    _step = float(np.ptp(_xz, axis=0).max() or 512.0) * 1.1
    _side = int(math.ceil(math.sqrt(args.scale)))
    _tiles = [(i % _side, i // _side) for i in range(args.scale)]
    _copies = []
    for _tx, _tz in _tiles[1:]:
        for _b in json.loads(json.dumps(brushes)):
            _p = _b.get('pos') or [0.0, 0.0, 0.0]
            _b['pos'] = [_p[0] + _tx * _step, _p[1], _p[2] + _tz * _step]
            if _b.get('geometry'):
                from engine import brush_geometry as _bg
                _bg.translate_brush(_b, (_tx * _step, 0.0, _tz * _step))
            _b['id'] = '%s-%d-%d' % (_b.get('id'), _tx, _tz)
            _copies.append(_b)
    brushes = brushes + _copies
etable = EntityTable()
hidden = etable.begin_frame([], epoch=1)


def _config(table):
    _mask, slots = table.shown()
    return {"render_mode": RENDER_MODE_LIT, "brush_display_mode": "Textured",
            "play_mode": False, "grid_visible": False,
            "camera_distance_cull": False, "shadows_enabled": False,
            "render_table": table, "all_brush_slots": slots.astype(np.int32),
            "entity_table": etable,
            "visible_thing_slots": np.arange(0, dtype=np.int32),
            "thing_hidden": hidden, "all_brushes": brushes, "all_things": []}


table = RenderTable()
table.begin_frame(brushes, epoch=1)
peer = RenderTable()
peer.begin_frame(brushes, epoch=1)
configs = {id(table): _config(table), id(peer): _config(peer)}

try:
    # As EditorState holds them (M4): a list that says when its rows change.
    from engine.row_list import RowList as _RowList        # noqa: E402
    brushes = _RowList(brushes)
except ImportError:                     # a tree before M4
    pass
_first = brushes[:len(brushes) // max(1, args.scale)]
pos = np.array([b.get("pos", (0, 0, 0)) for b in _first], dtype=float)
centre = pos.mean(axis=0)
extent = float(np.ptp(pos[:, [0, 2]], axis=0).max()) or 512.0
projection = glm.perspective(glm.radians(70.0), width / height, 1.0, 40000.0)


def _view(angle):
    eye = glm.vec3(centre[0] + 0.9 * extent * math.sin(angle),
                   centre[1] + 0.45 * extent,
                   centre[2] + 0.9 * extent * math.cos(angle))
    return glm.lookAt(eye, glm.vec3(*centre), glm.vec3(0, 1, 0)), eye


source = bp.RuntimeOverrides()
bp.register_runtime(source)
boxes = [i for i, b in enumerate(brushes) if not b.get('geometry')]
for i in boxes[:512]:
    brushes[i]['is_mover'] = True
homes = {i: list(brushes[i]['pos']) for i in boxes[:512]}
camera = {'angle': 0.0, 'cull': False}
current = {'table': table}
from engine.logic_render import LogicRender            # noqa: E402
_culler = LogicRender.__new__(LogicRender)
epoch = {'n': 10}


def _turning_view(angle):
    eye = glm.vec3(centre[0], centre[1] + 0.15 * extent, centre[2])
    target = eye + glm.vec3(math.sin(angle), -0.3, math.cos(angle))
    return glm.lookAt(eye, target, glm.vec3(0, 1, 0)), eye


_logic_ms = []


def _logic_side(tab, config):
    """The logic thread's share of a frame: the table brought up to date
    (RenderTable.begin_frame) and the frustum cull (LogicRender.cull_brushes)
    for the view this frame draws."""
    started = time.perf_counter()
    tab.begin_frame(brushes, epoch=epoch['n'])
    keep, all_slots = tab.shown()
    config['all_brush_slots'] = all_slots
    if camera['cull']:
        view, eye = _turning_view(camera['angle'])
    else:
        view, eye = _view(camera['angle'])
    planes = _culler.extract_frustum_planes(projection * view)
    slots = _culler.cull_brushes(tab, planes, keep)
    _logic_ms.append((time.perf_counter() - started) * 1e3)
    return view, eye, slots


def frame():
    tab = current['table']
    config = configs[id(tab)]
    if args.logic:
        view, eye, slots = _logic_side(tab, config)
    elif camera['cull']:
        view, eye = _turning_view(camera['angle'])
        planes = _culler.extract_frustum_planes(projection * view)
        bounds = tab.bounds[:tab.count]
        visible = np.flatnonzero(LogicRender.aabb_in_frustum_bounds(planes, bounds))
        slots = visible.astype(np.int32)
    else:
        view, eye = _view(camera['angle'])
        slots = config["all_brush_slots"]
    t0 = time.perf_counter()
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=slots)
    spent = (time.perf_counter() - t0) * 1e3
    gl.glFinish()
    return spent


def _move(rows, k, tab):
    for i in rows:
        h = homes[i]
        source.set(brushes[i], centre=(h[0], h[1] + 8.0 * ((k + i) % 7), h[2]))
    tab.refresh_transforms(brushes, np.asarray(rows, dtype=np.intp))


def still(k):
    return None


def camera_orbit(k):
    camera['cull'] = True
    camera['angle'] = 0.02 * k


def one_mover(k):
    _move(boxes[:1], k, table)


def movers64(k):
    _move(boxes[:64], k, table)


def movers512(k):
    _move(boxes[:512], k, table)


def selection(k):
    from editor.selection_overlay import describe_selection
    chosen = describe_selection(brushes[boxes[k % 2]])
    for tab in (table, peer):
        configs[id(tab)]['primary_selection'] = chosen


def editing(k):
    rows = boxes[(k * 32) % max(1, len(boxes) - 32):][:32]
    for i in rows:
        p = brushes[i]['pos']
        brushes[i]['pos'] = [p[0], p[1] + (2.0 if k % 2 else -2.0), p[2]]
    epoch['n'] += 1
    # Both double-buffered tables hear of every edit (the render journal).
    for tab in (table, peer):
        tab.begin_frame(brushes, epoch=epoch['n'],
                        dirty_objects={id(brushes[i]) for i in rows})


def alternating(k):
    tab = peer if k % 2 else table
    current['table'] = tab
    _move(boxes[:64], k, tab)


SCENARIOS = [('still', still), ('camera', camera_orbit), ('one_mover', one_mover),
             ('movers64', movers64), ('movers512', movers512),
             ('selection', selection), ('editing', editing),
             ('alternating', alternating)]


def _held_kb():
    """What the renderer keeps for box instances on the CPU side: M3's
    stream mirrors and slot matrices, or the pre-M3 staging buffer. (Each
    stream's GPU buffer is the size of its mirror.)"""
    total = 0
    for stream in getattr(renderer, '_brush_streams', {}).values():
        total += sum(a.nbytes for a in (stream.mirror, stream.slots, stream.spare,
                                         stream.payload))
    for cache in getattr(renderer, '_slot_transform_caches', {}).values():
        total += sum(a.nbytes for a in (cache.models, cache.normals, cache.bounds,
                                         cache.rot, cache.stamp) if a is not None)
    staging = getattr(renderer, '_brush_instance_data', None)
    if staging is not None:
        total += staging.nbytes
    total += renderer._brush_mat_buf.nbytes + renderer._brush_nmat_buf.nbytes
    return total / 1024.0


def _array_bytes(value):
    if isinstance(value, dict):
        return sum(_array_bytes(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(_array_bytes(v) for v in value)
    return getattr(value, 'nbytes', 0)


def _memo_kb():
    """What the renderer keeps of the passes' derived arrays (M3.1): every
    entry's arrays and its copy of the visible rows."""
    total = 0
    for tables in getattr(renderer, '_pass_memos', {}).values():
        for entry in tables.values():
            total += _array_bytes(entry[3]) + _array_bytes(entry[4])
    return total / 1024.0


def scenario(label, step, frames):
    for k in range(10):
        step(k)
        frame()
    # Timing (nothing else counted).
    times = []
    del _logic_ms[:]
    _detect = getattr(_change_detection, 'DETECT', None)
    if _detect is not None:
        _detect.reset()
    hits0 = getattr(renderer, 'pass_memo_hits', 0)
    misses0 = getattr(renderer, 'pass_memo_misses', 0)
    for k in range(frames):
        step(10 + k)
        times.append(frame())
    detect = ({site: (v[0] / frames, v[1] / frames / 1e3) for site, v in _detect.sites.items()}
              if _detect is not None else {})
    logic_ms = statistics.median(_logic_ms) if _logic_ms else None
    memo_hits = getattr(renderer, 'pass_memo_hits', 0) - hits0
    memo_misses = getattr(renderer, 'pass_memo_misses', 0) - misses0
    # GL calls and uploads.
    state['calls'].clear()
    state['bytes'].clear()
    _counting(True)
    for k in range(frames):
        step(10 + frames + k)
        frame()
    _counting(False)
    calls = dict(state['calls'])
    sent = dict(state['bytes'])
    # Python calls.
    prof = cProfile.Profile()
    for k in range(frames):
        step(10 + 2 * frames + k)
        prof.enable()
        frame()
        prof.disable()
    py_calls = pstats.Stats(prof).total_calls / frames
    if args.profile == label:
        import io
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats(
            os.environ.get('PROFILE_SORT', 'tottime')).print_stats(int(os.environ.get('PROFILE_TOP', '22')))
        for line in buf.getvalue().splitlines():
            if '/' in line or '{' in line:
                print('   ' + line.replace(tree + '/', '')[:150])
    # Memory: what the frames allocate at their peak, and keep.
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    for k in range(frames):
        step(10 + 3 * frames + k)
        frame()
    current_mem, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gl_calls = sum(v for key, v in calls.items()
                   if key not in ('upload', 'instance_upload'))
    out = {
        'render_ms_p50': statistics.median(times), 'render_ms_min': min(times),
        'instance_kb': sent.get('instance', 0) / frames / 1024.0,
        'instance_uploads': calls.get('instance_upload', 0) / frames,
        'upload_kb': sent.get('all', 0) / frames / 1024.0,
        'uploads': calls.get('upload', 0) / frames,
        'gl_calls': gl_calls / frames,
        'gl_by_name': {k: v / frames for k, v in sorted(calls.items())},
        'draws': (calls.get('glDrawArrays', 0) + calls.get('glDrawArraysInstanced', 0)
                  + calls.get('glDrawElements', 0)
                  + calls.get('glDrawElementsInstanced', 0)
                  + calls.get('glMultiDrawElements', 0)) / frames,
        'py_calls': py_calls,
        'alloc_peak_kb': (peak - base) / 1024.0,
        'kept_kb': (current_mem - base) / 1024.0,
        'held_kb': _held_kb(),
        'memo_kb': _memo_kb(),
        # M4: rows examined and microseconds spent establishing what
        # changed, a frame, per site; and the logic side's share (--logic).
        'detect': detect,
        'detect_rows': sum(v[0] for v in detect.values()),
        'detect_us': sum(v[1] for v in detect.values()),
        'logic_ms_p50': logic_ms,
        # M3.1: derived pass arrays reused / rebuilt, a frame.
        'memo_hits': memo_hits / frames,
        'memo_misses': memo_misses / frames,
    }
    print('%-11s render %6.2f ms (min %6.2f)  instance %7.1f KB %4.1f up  all %7.1f KB '
          '%4.1f up  gl %6.1f  draws %5.1f  py %7.0f  peak %7.1f KB  kept %6.1f KB  held %7.1f KB'
          % (label, out['render_ms_p50'], out['render_ms_min'], out['instance_kb'],
             out['instance_uploads'], out['upload_kb'], out['uploads'], out['gl_calls'],
             out['draws'], out['py_calls'], out['alloc_peak_kb'], out['kept_kb'],
             out['held_kb']))
    if detect or logic_ms is not None:
        print('%-11s change detection a frame: %.0f rows, %.1f us%s  [%s]'
              % ('', out['detect_rows'], out['detect_us'],
                 '' if logic_ms is None else ';  logic side %.3f ms' % logic_ms,
                 ', '.join('%s %.0f/%.1f' % (k, v[0], v[1])
                           for k, v in sorted(detect.items()))))
    if memo_hits or memo_misses:
        print('%-11s derived arrays a frame: %.1f reused, %.1f rebuilt; kept %.1f KB'
              % ('', out['memo_hits'], out['memo_misses'], out['memo_kb']))
    return out


print('%s  %s  brushes %d (convex %d, boxes %d), %dx%d'
      % (os.path.basename(tree), args.map, len(brushes),
         sum(1 for b in brushes if b.get('geometry')), len(boxes), width, height))
results = {}
only = set(filter(None, args.only.split(',')))
for label, step in SCENARIOS:
    if only and label not in only:
        continue
    # Every scenario starts from the same state: nothing overridden, both
    # tables re-read from the brushes.
    camera['angle'] = 0.0
    camera['cull'] = False
    current['table'] = table
    source.clear()
    epoch['n'] += 1
    for tab in (table, peer):
        configs[id(tab)].pop('primary_selection', None)
        tab.begin_frame(brushes, epoch=epoch['n'])
        configs[id(tab)]['all_brush_slots'] = tab.shown()[1].astype(np.int32)
    results[label] = scenario(label, step, args.frames)
source.clear()
renderer.cleanup()
if args.json:
    with open(args.json, 'w') as fh:
        json.dump({'map': args.map, 'tree': tree, 'results': results}, fh, indent=1)
del keep
