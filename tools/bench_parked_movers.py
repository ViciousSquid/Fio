"""What movers and doors Big World has parked cost a tick.

    python tools/bench_parked_movers.py TREE [--json OUT.json]

A level like a streamed one: a floor where the player is, and far away (in
cells Big World would not keep resident) 64 linear movers cycling, 16
spinners and 34 doors opened at the start. Two scenarios:

* ``awake``: nothing parked -- every row is walked and published;
* ``parked``: every far row carries Big World's parking mark
  (``engine.spatial.PARKED_HIDDEN_KEY``, the live ``hidden`` flag and a
  VISIBILITY touch, as ``BigWorldSession._set_brush_active`` leaves it).

Reports, over 900 ticks after 60 of warm-up, per tick: ``tick`` (the mover
table's ``_update_movers`` + ``_update_doors``, publication included),
``query`` (a batch of 64 floor rays near the player through the spatial
grid, what the AI asks), their ``calls`` (cProfile) and ``stamps`` (runtime
placement stamps a tick: what makes every reader's caches look again).

A tree without parked-row suspension walks parked rows as any other: run
the trees interleaved (``taskset -c 3``) and compare.
"""
import argparse
import cProfile
import gc
import json
import os
import pstats
import statistics
import sys
import time

DT = 1.0 / 60.0


def _box(name, pos, size, **extra):
    brush = {'id': name, 'name': name, 'pos': [float(v) for v in pos],
             'size': [float(v) for v in size], 'textures': {}}
    brush.update(extra)
    return brush


def level():
    brushes = [_box('floor', (0, -20, 0), (2000, 40, 2000))]
    far = []
    for i in range(64):
        far.append(_box('m%d' % i, (20000 + (i % 8) * 300, 40, (i // 8) * 300),
                        (96, 32, 96), is_mover=True, start_on=True,
                        direction=[0, 1, 0], distance=64.0 + i, speed=30.0 + i))
    for i in range(16):
        far.append(_box('s%d' % i, (24000 + i * 300, 64, 0), (160, 16, 16),
                        is_mover=True, start_on=True, rotate=True,
                        rot_axis=[0, 1, 0], distance=32.0, speed=45.0 + i))
    for i in range(34):
        far.append(_box('d%d' % i, (20000 + i * 96, 64, -1600), (64, 128, 16),
                        is_door=True, door_speed=96.0 + i, door_distance=96.0,
                        door_direction='up', open_time=3.0))
    return brushes + far, far


class World:
    def __init__(self, parked):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])
        import numpy as np
        from editor.editor_state import EditorState
        from engine import brush_placement
        from engine.change_journal import VISIBILITY, touch
        from engine.logic_thread import LogicThread
        from engine.physics import SpatialGrid
        from engine.spatial import PARKED_HIDDEN_KEY
        from engine.threaded_game_state import ThreadedGameState
        self.placement = brush_placement
        state = EditorState()
        state.brushes, far = level()
        logic = LogicThread(ThreadedGameState(), state)
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        logic.io_manager = None
        self.runtime = logic.mover_runtime
        self.runtime._init_movers()
        self.runtime._init_doors()
        for s in self.runtime.door_states.values():
            s['state'] = 'opening'
        self.grid = SpatialGrid()
        self.grid.populate(state.brushes)
        self.logic = logic
        if parked:
            for brush in far:
                brush[PARKED_HIDDEN_KEY] = brush.get('hidden', False)
                brush['hidden'] = True
                brush['bw_active'] = False
                touch(brush, VISIBILITY)
        angles = np.linspace(0.0, 6.283, 64)
        self.qx = np.cos(angles) * 300.0
        self.qz = np.sin(angles) * 300.0

    def tick(self):
        self.runtime._update_movers(DT)
        self.runtime._update_doors(DT)

    def query(self):
        return self.grid.raycast_down_batch(self.qx, self.qz, 500.0)

    def close(self):
        self.runtime._reset_movers()
        self.runtime._reset_doors()
        self.logic.stop()
        self.logic = self.runtime = self.grid = None
        gc.collect()


def measure(parked):
    world = World(parked)
    for _ in range(60):
        world.tick()
        world.query()
    prof_tick, prof_query = cProfile.Profile(), cProfile.Profile()
    for _ in range(60):
        prof_tick.enable()
        world.tick()
        prof_tick.disable()
        prof_query.enable()
        world.query()
        prof_query.disable()
    calls = {name: pstats.Stats(p).total_calls / 60.0
             for name, p in (('tick', prof_tick), ('query', prof_query))}
    stamp = world.placement.runtime_generation
    ticks, queries = [], []
    clock = time.perf_counter
    for _ in range(900):
        t0 = clock()
        world.tick()
        t1 = clock()
        world.query()
        t2 = clock()
        ticks.append((t1 - t0) * 1e6)
        queries.append((t2 - t1) * 1e6)
    stamps = (world.placement.runtime_generation - stamp) / 900.0
    world.close()
    return {'tick': ticks, 'query': queries, 'tick_calls': calls['tick'],
            'query_calls': calls['query'], 'stamps': stamps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tree')
    ap.add_argument('--json')
    args = ap.parse_args()
    tree = os.path.abspath(args.tree)
    sys.path.insert(0, tree)
    os.chdir(tree)
    out = {}
    for name, parked in (('awake', False), ('parked', True)):
        r = measure(parked)
        out[name] = r
        print('%-7s tick %7.1f us (min %6.1f, %6.0f calls)  query %6.1f us '
              '(min %5.1f, %4.0f calls)  stamps/tick %.2f' % (
                  name, statistics.median(r['tick']), min(r['tick']), r['tick_calls'],
                  statistics.median(r['query']), min(r['query']), r['query_calls'],
                  r['stamps']))
    if args.json:
        with open(args.json, 'w') as fh:
            json.dump(out, fh)


if __name__ == '__main__':
    main()
