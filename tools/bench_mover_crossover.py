"""Where dense mover publication starts to pay (M4).

    python tools/bench_mover_crossover.py [--tree PATH] [--json OUT]

``MoverTable.publish`` writes a render table either the rows the tick
marked as published (marks mode) or, for a group with at most
``mover_table.DENSE_SLACK_ROWS`` rows unmarked, every row of the group,
logged as written (dense mode: no picking, but rows that did not move are
rebuilt and re-uploaded downstream). Which is cheaper depends on the share of rows that
moved, the group's size and the machine -- so it is measured, not assumed.

For each group size and moving share, with each mode forced, this times:

* ``publish`` alone (the logic thread's share);
* the renderer's share for the rows the publication logged: the kept
  matrices brought up to date (``SlotTransforms.sync``) and the instance
  rows packed for them (``InstancingMixin._stream_block``) -- the CPU part of
  what M3's streams do with a logged row. The upload itself is GL time,
  not counted (on this host ~0.02 us a KB; a mobile driver differs).

and which mode is cheaper end to end, beside what the current rule picks
and costs. Run it on the target (a Snapdragon 8cx Gen 3 here is the one
that matters) and set ``DENSE_SLACK_ROWS`` from what it prints.
"""
import argparse
import json
import os
import statistics
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
ap.add_argument('--json')
ap.add_argument('--sizes', default='8,32,128,512')
ap.add_argument('--shares', default='0.05,0.1,0.25,0.4,0.5,0.6,0.75,0.9,1.0')
ap.add_argument('--publishes', type=int, default=300)
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path.insert(0, tree)
sys.path.insert(0, os.path.join(tree, 'tools'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import numpy as np                                        # noqa: E402
import perf_probes                                        # noqa: E402
perf_probes._qt()
from editor.editor_state import EditorState               # noqa: E402
from engine import mover_table                            # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player                          # noqa: E402
from engine.render_table import RenderTable               # noqa: E402
from engine.renderer.core.slot_transforms import SlotTransforms   # noqa: E402
from engine.renderer.forward.instancing import InstancingMixin    # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402


def _world(count, share):
    """*count* linear movers, the first *share* of them moving, among
    static scenery."""
    moving = int(round(count * share))
    out = []
    for k in range(count):
        out.append({"id": "m%d" % k, "name": "m%d" % k, "pos": [k * 150.0, 0.0, 0.0],
                    "size": [64.0, 64.0, 64.0], "is_mover": True,
                    "start_on": k < moving, "speed": 64.0 + k, "distance": 512.0,
                    "direction": [0, 1, 0]})
    for k in range(count * 4):
        out.append({"id": "s%d" % k, "name": "s%d" % k,
                    "pos": [(k % 40) * 90.0, -40.0, (k // 40) * 90.0 - 200.0],
                    "size": [80.0, 16.0, 80.0]})
    return out


def _measure(count, share, slack, dense_share=2.0):
    mover_table.DENSE_SLACK_ROWS = slack
    mover_table.DENSE_SHARE = dense_share
    state = EditorState()
    state.brushes = _world(count, share)
    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player = Player(1.0, 3.0)
    logic.session_runtime.play_mode = True
    logic.world_runtime.build_entity_caches()
    logic.io_manager = None
    runtime = logic.mover_runtime
    runtime._init_movers()
    runtime._init_doors()
    movers = runtime._mover_table
    tables = [RenderTable(), RenderTable()]
    caches = []
    for table in tables:
        table.sync(state.brushes, 1)
        cache = SlotTransforms(table)
        cache.sync(table)
        caches.append(cache)
    delta = 1.0 / 60.0
    publish, downstream, logged = [], [], []
    for i in range(40 + args.publishes):
        table, cache = tables[i % 2], caches[i % 2]   # the two buffers in turn
        movers.sync()
        runtime._update_movers(delta)
        version = table.transform_version
        t0 = time.perf_counter_ns()
        movers.publish(logic, table)
        t1 = time.perf_counter_ns()
        rows = table.transforms_since(version)
        rows = np.arange(table.count) if rows is None else rows
        before = cache.version
        cache.sync(table)
        changed = cache.rows_since(before)
        if changed is not None and len(changed):
            InstancingMixin._stream_block(cache, changed, 0.0, 0.0, True)
        t2 = time.perf_counter_ns()
        if i >= 40:
            publish.append((t1 - t0) / 1e3)
            downstream.append((t2 - t1) / 1e3)
            logged.append(len(rows))
    logic.stop()
    return {'publish_us': statistics.median(publish),
            'downstream_us': statistics.median(downstream),
            'total_us': statistics.median(p + d for p, d in zip(publish, downstream)),
            'logged_rows': statistics.median(logged)}


results = []
rule = mover_table.DENSE_SLACK_ROWS
rule_share = mover_table.DENSE_SHARE
for count in map(int, args.sizes.split(',')):
    for share in map(float, args.shares.split(',')):
        marks = _measure(count, share, -1)               # never dense
        dense = _measure(count, share, 1 << 30)          # always dense
        moving = int(round(count * share))
        picked = ('dense' if count - moving <= rule or moving >= rule_share * count
                  else 'marks')
        results.append({'movers': count, 'share': share, 'marks': marks,
                        'dense': dense, 'rule': picked})
        best = 'dense' if dense['total_us'] < marks['total_us'] else 'marks'
        print('%4d movers  %3.0f%% moving   marks: publish %6.1f + downstream %6.1f = %6.1f us'
              ' (%4.0f rows)   dense: %6.1f + %6.1f = %6.1f us (%4.0f rows)   cheaper %s;'
              ' rule (slack %d, share %.2f) picks %s%s'
              % (count, share * 100, marks['publish_us'], marks['downstream_us'],
                 marks['total_us'], marks['logged_rows'], dense['publish_us'],
                 dense['downstream_us'], dense['total_us'], dense['logged_rows'],
                 best, rule, rule_share, picked,
                 '' if picked == best else '  (%+.1f us)' % (
                     (dense if picked == 'dense' else marks)['total_us']
                     - min(dense['total_us'], marks['total_us']))))
    print()
mover_table.DENSE_SLACK_ROWS = rule
mover_table.DENSE_SHARE = rule_share

if args.json:
    with open(args.json, 'w') as handle:
        json.dump(results, handle, indent=1)
