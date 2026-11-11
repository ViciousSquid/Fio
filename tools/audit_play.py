"""Whole-engine audit: one map played in the real editor, measured as shipped.

    xvfb-run -a python tools/audit_play.py MAP [--seconds 12] [--json OUT]
                                                [--ready FILE] [--tree PATH]
                                                [--loop] [--hide-2d]
                                                [--span MODULE:CLASS.METHOD ...]

The editor's MainWindow loads *MAP* (a file in ``maps/``) and enters play:
the logic thread free-runs at 60 Hz, the monster AI runs on its own thread,
and this script paints at up to 60 Hz with scripted input (forward, strafe,
mouse look, firing). Over the measured window it records:

* load and play-entry time, the first frame, and resident memory at each
  stage plus a sample every two seconds of play;
* CPU per thread (cores busy: the main/paint thread, the logic thread, the
  monster AI, the GL driver's threads);
* logic tick cost and pacing (how far each tick starts from its 60 Hz slot);
* paint cost and rate;
* garbage collections by generation and their pauses, the objects tracked
  and frozen (``engine/gc_policy.py``).

``--loop`` measures the frame loop as the editor runs it instead of painting
from this script: Qt's event loop, the view's own 16 ms timer
(``update_loop``), Qt's paint of the 3D view and of whatever else a frame
repaints (the 2D views, the Debug Console), the switch interval ``main.py``
sets. It reports the frame rate and interval, and the main thread's time by
part: ``update_loop``, ``paintGL``, the 2D views' ``paintEvent``, and the
rest (Qt composing the window, event delivery). ``--hide-2d`` closes the 2D
views' dock first, to price what they cost a frame in play. ``--span
engine.terrain:Terrain._upload_chunk`` (repeatable) times one more method
inside the frame, reported beside the parts. The default mode
paints from this script as well as from Qt (a published frame is painted
twice), so its ``paint_ms`` is one paint's cost, not a frame's.

A map without a PlayerStart cannot be played (the editor says so in a
dialogue): it is measured in the editor view instead, ``"played": false``.

``--ready FILE`` is written when the measured window starts, so a profiler
can attach then (``py-spy record --pid ... --threads --gil``). Under software
GL the paint numbers measure llvmpipe, not a GPU: compare runs on one host,
and treat GL-bound costs as host-specific. ``settings.ini`` is restored.
"""

import argparse
import gc
import json
import os
import sys
import threading
import time

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument('map')
ap.add_argument('--seconds', type=float, default=12.0)
ap.add_argument('--json')
ap.add_argument('--ready')
ap.add_argument('--loop', action='store_true',
                help="measure Qt's own frame loop, not paints made from this script")
ap.add_argument('--hide-2d', action='store_true',
                help='close the 2D views dock before measuring')
ap.add_argument('--span', action='append', default=[], metavar='MODULE:CLASS.METHOD',
                help='with --loop: also time this method (repeatable)')
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path[:0] = [tree, os.path.join(tree, 'tools')]
os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
settings = os.path.join(tree, 'settings.ini')
saved = open(settings, 'rb').read()


def rss_mb():
    with open('/proc/self/statm') as f:
        return int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE') / 2**20


def thread_cpu():
    """native thread id -> (name, CPU seconds), from /proc (Linux)."""
    names = {t.native_id: t.name for t in threading.enumerate()}
    tick = os.sysconf('SC_CLK_TCK')
    out = {}
    for tid in os.listdir('/proc/self/task'):
        try:
            with open('/proc/self/task/%s/stat' % tid) as f:
                parts = f.read().rsplit(')', 1)[1].split()
            with open('/proc/self/task/%s/comm' % tid) as f:
                comm = f.read().strip()
        except OSError:
            continue
        out[int(tid)] = (names.get(int(tid), comm), (int(parts[11]) + int(parts[12])) / tick)
    return out


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


rss_start = rss_mb()
from PyQt5.QtCore import Qt                                   # noqa: E402
from PyQt5.QtWidgets import QApplication                      # noqa: E402
app = QApplication.instance() or QApplication([])
import bench_compare as bc                                    # noqa: E402
import engine.logic_thread as lt                              # noqa: E402

ticks = []
real_tick = lt.LogicThread._tick


def timed_tick(self, delta):
    started = time.perf_counter_ns()
    real_tick(self, delta)
    ticks.append((started, time.perf_counter_ns() - started))


lt.LogicThread._tick = timed_tick
spans = {}


def span(cls, name, label):
    """Record (start, seconds, CPU seconds of the calling thread) of every
    call of ``cls.name`` under *label*."""
    real, bucket = getattr(cls, name), spans.setdefault(label, [])

    def timed(self, *a, **k):
        started, cpu = time.perf_counter(), time.thread_time()
        try:
            return real(self, *a, **k)
        finally:
            bucket.append((started, time.perf_counter() - started,
                           time.thread_time() - cpu))

    setattr(cls, name, timed)


loop_input = {'on': False, 'index': 0}
if args.loop:
    # As main.py runs: its switch interval, and the classes' frame entry
    # points timed before any instance exists.
    sys.setswitchinterval(0.001)
    from engine.terrain import Terrain                        # noqa: E402
    Terrain.BACKGROUND_BUILDS = True
    from engine.qt_game_view import QtGameView                # noqa: E402
    from editor.view_2d import View2D                         # noqa: E402
    from editor.main_window import MainWindow                 # noqa: E402
    span(QtGameView, 'paintGL', 'paintGL')
    span(View2D, 'paintEvent', 'view_2d paintEvent')
    span(MainWindow, 'update_views', 'update_views')
    real_update_loop = QtGameView.update_loop

    def driven_update_loop(self):
        if loop_input['on']:
            keys, dx, dy, fire = bc._script(loop_input['index'], Qt.Key_W, Qt.Key_A)
            loop_input['index'] += 1
            self.editor.keys_pressed = keys
            real_update_loop(self)
            self.game_state.set_mouse_delta(dx, dy)
            if fire:
                self.game_state.queue_shot()
        else:
            real_update_loop(self)

    QtGameView.update_loop = driven_update_loop
    span(QtGameView, 'update_loop', 'update_loop')
    FRAME_PARTS = tuple(spans)
    import importlib                                          # noqa: E402
    for spec in args.span:
        module, _, path = spec.partition(':')
        owner = importlib.import_module(module)
        *owners, method = path.split('.')
        for name in owners:
            owner = getattr(owner, name)
        span(owner, method, path)
collections = []
gc_started = {}


def gc_watch(phase, info):
    if phase == 'start':
        gc_started['t'] = time.perf_counter_ns()
    elif 't' in gc_started:
        collections.append((info['generation'], (time.perf_counter_ns() - gc_started['t']) / 1e6))


try:
    bench = bc.Bench(app)
    window, view = bench.window, bench.view
    rss_editor = rss_mb()
    t0 = time.perf_counter()
    window.load_level_file(os.path.join(tree, 'maps', args.map))
    app.processEvents()
    load_s = time.perf_counter() - t0
    rss_loaded = rss_mb()
    if args.hide_2d:
        window.right_dock.hide()
        app.processEvents()
    played = window.state.resolve_player_start() is not None
    t0 = time.perf_counter()
    if played:
        window.enter_play_mode()
    app.processEvents()
    view.makeCurrent()
    p0 = time.perf_counter()
    view.paintGL()
    first_paint_ms = (time.perf_counter() - p0) * 1e3
    view.doneCurrent()
    play_entry_s = time.perf_counter() - t0
    state = view.game_state
    settle = time.perf_counter() + 2.0
    while time.perf_counter() < settle:
        app.processEvents()
        time.sleep(0.005)
    del ticks[:]
    gc.callbacks.append(gc_watch)
    cpu0 = thread_cpu()
    if args.ready:
        with open(args.ready, 'w') as f:
            f.write('measuring')
    paints, paint_starts, rss_timeline = [], [], []
    for bucket in spans.values():
        del bucket[:]
    if args.loop:
        from PyQt5.QtCore import QTimer                       # noqa: E402
        rss_timer = QTimer()
        rss_timer.timeout.connect(lambda: rss_timeline.append(round(rss_mb(), 1)))
        rss_timer.start(2000)
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
        loop_input['on'] = True
        app.exec_()
        loop_input['on'] = False
        rss_timer.stop()
        paint_starts = [b[0] for b in spans['paintGL']]
        paints = [b[1] * 1e3 for b in spans['paintGL']]
    end = time.perf_counter() + (0.0 if args.loop else args.seconds)
    next_paint = last_rss = time.perf_counter()
    index = 0
    while time.perf_counter() < end:
        now = time.perf_counter()
        if now >= next_paint:
            next_paint = max(next_paint + 1.0 / 60.0, now)
            keys, dx, dy, fire = bc._script(index, Qt.Key_W, Qt.Key_A)
            index += 1
            state.set_keys(keys)
            state.set_mouse_delta(dx, dy)
            if fire:
                state.queue_shot()
            view.makeCurrent()
            p0 = time.perf_counter()
            view.paintGL()
            paints.append((time.perf_counter() - p0) * 1e3)
            paint_starts.append(p0)
            view.doneCurrent()
            app.processEvents()
            if now - last_rss >= 2.0:
                last_rss = now
                rss_timeline.append(round(rss_mb(), 1))
        else:
            time.sleep(min(0.002, next_paint - now))
    cpu1 = thread_cpu()
    gc.callbacks.remove(gc_watch)
    tracked, frozen = len(gc.get_objects()), gc.get_freeze_count()
    rss_playing = rss_mb()
    if played:
        window.enter_play_mode()
    app.processEvents()

    cores = {}
    for tid, (name, seconds) in cpu1.items():
        key = name.rstrip('0123456789-') if name.startswith(('llvmpipe', 'python')) else name
        cores[key] = cores.get(key, 0.0) + (seconds - cpu0.get(tid, (name, 0.0))[1]) / args.seconds
    starts = [s for s, _ in ticks]
    jitter = [abs((b - a) / 1e6 - 1000.0 / 60.0) for a, b in zip(starts, starts[1:])]
    tick_ms = [d / 1e6 for _, d in ticks]
    intervals = [(b - a) * 1e3 for a, b in zip(paint_starts, paint_starts[1:])]
    report = {
        'map': args.map, 'seconds': args.seconds, 'played': played,
        'mode': 'loop' if args.loop else 'script', '2d_views': not args.hide_2d,
        'load_s': load_s, 'play_entry_s': play_entry_s, 'first_paint_ms': first_paint_ms,
        'rss_mb': {'start': rss_start, 'editor': rss_editor, 'loaded': rss_loaded,
                   'playing': rss_playing, 'timeline': rss_timeline},
        'cpu_cores_by_thread': {k: round(v, 3) for k, v in sorted(cores.items(), key=lambda kv: -kv[1])
                                if v > 0.005},
        'ticks_per_s': len(ticks) / args.seconds,
        'tick_ms': {'p50': pct(tick_ms, .5), 'p99': pct(tick_ms, .99)},
        'tick_jitter_ms': {'p50': pct(jitter, .5), 'p95': pct(jitter, .95), 'p99': pct(jitter, .99)},
        'paints_per_s': len(paints) / args.seconds,
        'paint_ms': {'p50': pct(paints, .5), 'p95': pct(paints, .95), 'p99': pct(paints, .99)},
        'paint_interval_ms_p95': pct(intervals, .95),
        'gc': {'collections': {g: sum(1 for x, _ in collections if x == g) for g in (0, 1, 2)},
               'max_pause_ms': {g: max((d for x, d in collections if x == g), default=0.0)
                                for g in (0, 1, 2)},
               'objects_tracked': tracked, 'objects_frozen': frozen},
    }
    if args.loop:
        # A frame's wall time and the main thread's CPU, by part. update_views
        # runs inside update_loop (it only schedules the paints). What the
        # parts leave is Qt's: composing the window, delivering events, and,
        # of the wall time, waiting for the next timer tick.
        frames = max(1, len(paints))
        main_cpu = 1e3 * args.seconds * cores.get('MainThread', 0.0) / frames
        parts = {label: {'calls_per_frame': round(len(b) / frames, 2),
                         'wall_ms_per_frame': round(1e3 * sum(x[1] for x in b) / frames, 3),
                         'cpu_ms_per_frame': round(1e3 * sum(x[2] for x in b) / frames, 3),
                         'wall_ms_p50': round(1e3 * pct([x[1] for x in b], .5), 3),
                         'wall_ms_p95': round(1e3 * pct([x[1] for x in b], .95), 3)}
                 for label, b in spans.items()}
        for label, b in spans.items():
            parts[label]['wall_ms_max'] = round(1e3 * max((x[1] for x in b), default=0.0), 3)
        top = [k for k in FRAME_PARTS if k != 'update_views']
        report['frame'] = {
            'frames_per_s': len(paints) / args.seconds,
            'interval_ms': {'p50': pct(intervals, .5), 'p95': pct(intervals, .95),
                            'p99': pct(intervals, .99), 'max': max(intervals, default=0.0)},
            'parts': parts,
            'main_thread_cpu_ms_per_frame': round(main_cpu, 3),
            'qt_cpu_ms_per_frame': round(
                main_cpu - sum(parts[k]['cpu_ms_per_frame'] for k in top), 3),
            'qt_and_idle_wall_ms_per_frame': round(
                1e3 * args.seconds / frames
                - sum(parts[k]['wall_ms_per_frame'] for k in top), 3),
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
