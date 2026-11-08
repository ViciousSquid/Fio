"""Player collision benchmark (unified brush model, Stage 2c).

    python tools/bench_collision.py [--tree PATH] [--core N] [--ticks 900] [--json out.json]

What one player tick costs next to the shapes a level puts around it, with
nothing else running: the collision path is pure Python on the logic
thread, so this is the number that matters on a slow core (the Snapdragon
8cx Gen 3 target runs it several times slower than a desktop x86 core).
Pin it to one core (``--core``) and compare trees run the same way; the
figures are relative, not a device measurement.

Scenarios: the player walks along a row of eight shapes standing on a
floor -- static boxes, static angled brushes, box movers, angled movers,
spinning boxes, spinning angled brushes. Movers are driven the way the
tree under test drives them: through a runtime source where it has one
(Stage 2b and later), by writing ``pos`` / ``_rot_angle`` before that.

Reported per scenario: player tick p50 / p95 / p99 (µs), Python memory
blocks allocated per tick (tracemalloc, a separate pass), and over the
whole run the convex solves (``compute_windings``), collision-shape builds
and shape-frame builds -- which must stay at their first-tick values: a
moving or spinning brush is carried, never rebuilt.

``--tree`` runs another checkout (e.g. a Stage 1 worktree) with this file.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time
import tracemalloc

SCENARIOS = ('static_boxes', 'static_angled', 'box_movers', 'angled_movers',
             'spinning_boxes', 'spinning_angled')


def _percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _box(name, pos, size, **extra):
    brush = {'id': name, 'name': name, 'pos': [float(v) for v in pos],
             'size': [float(v) for v in size], 'textures': {}}
    brush.update(extra)
    return brush


def _angled(bg, name, pos, size, **extra):
    brush = _box(name, pos, size, **extra)
    bg.box_to_geometry(brush)
    # A ramp: cut the top at a slope, as the editor's clip tool does.
    bg.clip_brush(brush, [0.35, 1.0, 0.0], float(pos[1]) * 1.0 + 4.0)
    return brush


class Counters:
    """Counts the rebuilds a carried shape must never need."""

    def __init__(self, bg, bp):
        self.solves = 0
        self.shapes = 0
        self.frames = 0
        self._undo = []
        real_windings = bg.compute_windings

        def windings(*a, **k):
            self.solves += 1
            return real_windings(*a, **k)
        self._patch(bg, 'compute_windings', windings)
        convex = getattr(bg, 'ConvexGeometry', None)
        real_shape = getattr(convex, 'collision_shape', None)
        if real_shape is not None:
            def shape(geo):
                if geo._collision_shape is None:
                    self.shapes += 1
                return real_shape(geo)
            self._patch(convex, 'collision_shape', shape)
        frame = getattr(bp, 'Frame', None) if bp is not None else None
        if frame is not None:
            real_init = frame.__init__

            def init(f, *a, **k):
                self.frames += 1
                real_init(f, *a, **k)
            self._patch(frame, '__init__', init)

    def _patch(self, owner, name, value):
        self._undo.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def restore(self):
        for owner, name, value in reversed(self._undo):
            setattr(owner, name, value)
        self._undo = []

    def snapshot(self):
        return {'convex_solves': self.solves, 'collision_shape_builds': self.shapes,
                'frame_builds': self.frames}


def scenario(bg, kind):
    floor = _box('floor', (0, -20, 0), (4000, 40, 4000))
    extra = []
    for i in range(8):
        x = -700 + i * 200
        if kind == 'static_boxes':
            extra.append(_box('b%d' % i, (x, 40, 120), (96, 32, 96)))
        elif kind == 'static_angled':
            brush = _angled(bg, 'a%d' % i, (x, 40, 120), (96, 64, 96))
            bg.build_collision_mesh(brush)
            extra.append(brush)
        elif kind == 'box_movers':
            extra.append(_box('m%d' % i, (x, 40, 120), (96, 32, 96), is_mover=True))
        elif kind == 'angled_movers':
            extra.append(_angled(bg, 'am%d' % i, (x, 40, 120), (96, 64, 96), is_mover=True))
        elif kind == 'spinning_boxes':
            extra.append(_box('s%d' % i, (x, 40, 120), (160, 16, 16), is_mover=True,
                              rotate=True, rot_axis=[0, 1, 0]))
        elif kind == 'spinning_angled':
            extra.append(_angled(bg, 'sa%d' % i, (x, 40, 120), (96, 64, 96),
                                 is_mover=True, rotate=True, rot_axis=[0, 1, 0]))
    return floor, extra


def run(kind, ticks, measure_allocations):
    import glm
    from engine import brush_geometry as bg
    from engine.physics import SpatialGrid
    from engine.player import Player
    try:
        from engine import brush_placement as bp
        source = bp.RuntimeOverrides()
        bp.register_runtime(source)
    except (ImportError, AttributeError):
        bp = source = None
    floor, extra = scenario(bg, kind)
    statics = [floor] + [b for b in extra if not b.get('is_mover')]
    movers = [b for b in extra if b.get('is_mover')]
    grid = SpatialGrid()
    grid.populate(statics + movers)
    homes = [list(m['pos']) for m in movers]
    player = Player(-760.0, 120.0, angle=0.0)
    player.pos.y = 30.0
    move = glm.vec3(1.0, 0.0, 0.0)

    def step(tick):
        for i, m in enumerate(movers):
            y = 40.0 + 20.0 * math.sin(tick * 0.05 + i)
            angle = (30.0 + tick * 2.0) % 360.0 if m.get('rotate') else 0.0
            if source is not None:
                h = homes[i]
                source.set(m, centre=(h[0], y, h[2]),
                           spin=((0.0, 1.0, 0.0), angle) if angle else None)
            else:
                m['pos'][1] = y
                if angle:
                    m['_rot_angle'] = angle
        t0 = time.perf_counter()
        player.update(1.0 / 60.0, move, False, False, statics, movers=movers,
                      spatial_grid=grid)
        spent = time.perf_counter() - t0
        if player.pos.x > 900:
            player.pos.x = -760.0
        return spent

    warm = 60
    for tick in range(warm):
        step(tick)
    counters = Counters(bg, bp)
    try:
        samples = [step(warm + tick) * 1e6 for tick in range(ticks)]
        counts = counters.snapshot()
    finally:
        counters.restore()
    out = {'p50_us': round(statistics.median(samples), 2),
           'p95_us': round(_percentile(samples, 0.95), 2),
           'p99_us': round(_percentile(samples, 0.99), 2)}
    out.update(counts)
    if measure_allocations:
        tracemalloc.start()
        try:
            before = tracemalloc.take_snapshot()
            for tick in range(200):
                step(warm + ticks + tick)
            after = tracemalloc.take_snapshot()
        finally:
            tracemalloc.stop()
        blocks = sum(stat.count_diff for stat in after.compare_to(before, 'filename')
                     if stat.count_diff > 0)
        out['retained_blocks_per_tick'] = round(blocks / 200.0, 2)
    if source is not None:
        source.clear()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tree', default=None, help='checkout to benchmark (default: this one)')
    parser.add_argument('--core', type=int, default=None, help='pin to this CPU core')
    parser.add_argument('--ticks', type=int, default=900)
    parser.add_argument('--no-allocations', action='store_true')
    parser.add_argument('--json')
    args = parser.parse_args()
    tree = os.path.abspath(args.tree or os.path.join(os.path.dirname(__file__), '..'))
    out_path = os.path.abspath(args.json) if args.json else None
    os.chdir(tree)
    sys.path.insert(0, tree)
    if args.core is not None and hasattr(os, 'sched_setaffinity'):
        os.sched_setaffinity(0, {args.core})
    result = {'tree': tree, 'core': args.core, 'ticks': args.ticks, 'scenarios': {}}
    for kind in SCENARIOS:
        result['scenarios'][kind] = run(kind, args.ticks, not args.no_allocations)
        row = result['scenarios'][kind]
        print('%-16s p50 %7.2f  p95 %7.2f  p99 %7.2f us | solves %d  shapes %d  frames %d%s' % (
            kind, row['p50_us'], row['p95_us'], row['p99_us'], row['convex_solves'],
            row['collision_shape_builds'], row['frame_builds'],
            '' if 'retained_blocks_per_tick' not in row
            else ' | retained blocks/tick %.2f' % row['retained_blocks_per_tick']))
    if out_path:
        with open(out_path, 'w') as fh:
            json.dump(result, fh, indent=1)


if __name__ == '__main__':
    main()
