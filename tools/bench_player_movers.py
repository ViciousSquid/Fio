"""The player beside eight moving boxes (``bench_collision``'s ``box_movers``
scenario), on any checkout: Python calls per player tick and what the first
update after the movers moved costs over a second, cached one.

    python tools/bench_player_movers.py TREE [--kind box_movers] [--calls N]
    python tools/bench_player_movers.py TREE --trace OUT.json

*TREE* is a checkout (Stage 1 included: it has no runtime sources, so its
movers are moved by writing ``pos``, as Stage 1 did). The scenario and the
runtime source come from this checkout's ``tools``; the engine from *TREE*.

Reports, over 900 ticks after 60 of warm-up:

* ``calls/tick``: Python calls (cProfile) in one ``Player.update`` a tick --
  the update straight after the movers moved, as the gate's player probe
  times it. ``--calls N`` also lists the N most-called functions.
* ``first`` / ``second``: that update, and a second one from the same state
  with every AABB cached, in microseconds (min and median); ``miss share``
  is their difference per moving box -- what a moved box costs the first
  reader after the move.
* ``move``: the source update itself (not timed by the gate).

Timings are per-process medians: run trees interleaved, pinned to one core,
several times (``taskset -c 3``), and compare minima and medians.

``--trace OUT.json`` instead writes the player's state every tick (position,
velocity, on-ground, what it stands on) through every ``bench_collision``
scenario from three starting lanes, jumping now and then: two Stage 2 trees
that collide alike write byte-identical files (``cmp``).
"""
import argparse
import cProfile
import io
import math
import os
import pstats
import statistics
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tree')
    ap.add_argument('--kind', default='box_movers')
    ap.add_argument('--calls', type=int, default=0)
    ap.add_argument('--trace')
    args = ap.parse_args()
    tools = os.path.dirname(os.path.abspath(__file__))
    tree = os.path.abspath(args.tree)
    os.chdir(tree)
    sys.path.insert(0, tools)
    sys.path.insert(0, tree)
    import glm
    import bench_collision as bc
    import perf_probes as pp
    from engine import brush_geometry as bg
    from engine.physics import SpatialGrid
    from engine.player import Player

    source = pp._runtime_source()
    if args.trace:
        trace(args.trace, bc, bg, glm, source, SpatialGrid, Player)
        return
    floor, extra = bc.scenario(bg, args.kind)
    statics = [floor] + [b for b in extra if not b.get('is_mover')]
    movers = [b for b in extra if b.get('is_mover')]
    grid = SpatialGrid()
    grid.populate(statics + movers)
    homes = [list(m['pos']) for m in movers]
    move = glm.vec3(1.0, 0.0, 0.0)

    def run(profile):
        player = Player(-760.0, 120.0, angle=0.0)
        player.pos.y = 30.0
        prof = cProfile.Profile() if profile else None
        first, second, moving = [], [], []
        for tick in range(960):
            t0 = time.perf_counter()
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
            t1 = time.perf_counter()
            state = (glm.vec3(player.pos), glm.vec3(player.velocity), player.on_ground)
            if prof is not None and tick >= 60:
                prof.enable()
            player.update(1.0 / 60.0, move, False, False, statics, movers=movers,
                          spatial_grid=grid)
            if prof is not None and tick >= 60:
                prof.disable()
            t2 = time.perf_counter()
            if prof is None:
                # The same update again from the same state: all cached.
                player.pos, player.velocity, player.on_ground = (
                    glm.vec3(state[0]), glm.vec3(state[1]), state[2])
                t3 = time.perf_counter()
                player.update(1.0 / 60.0, move, False, False, statics, movers=movers,
                              spatial_grid=grid)
                t4 = time.perf_counter()
            if player.pos.x > 900:
                player.pos.x = -760.0
            if tick >= 60 and prof is None:
                moving.append((t1 - t0) * 1e6)
                first.append((t2 - t1) * 1e6)
                second.append((t4 - t3) * 1e6)
        return prof, first, second, moving

    prof, _, _, _ = run(True)
    stats = pstats.Stats(prof)
    calls = sum(v[1] for v in stats.stats.values()) / 900.0
    _, first, second, moving = run(False)
    name = os.path.basename(tree.rstrip('/'))
    f, s = statistics.median(first), statistics.median(second)
    print('%s %s: %d movers  calls/tick %.1f  first %.1f/%.1f  second %.1f/%.1f  '
          'miss share %.2f us per mover  move %.1f  (us, min/median)' % (
              name, args.kind, len(movers), calls, min(first), f, min(second), s,
              (f - s) / max(1, len(movers)), statistics.median(moving)))
    if args.calls:
        buf = io.StringIO()
        pstats.Stats(prof, stream=buf).sort_stats('calls').print_stats(args.calls)
        for line in buf.getvalue().splitlines():
            if '/' in line or '{' in line:
                print('   ' + line.replace(tree + '/', '')[:140])


def trace(out, bc, bg, glm, source, SpatialGrid, Player):
    import json
    res = {}
    for kind in bc.SCENARIOS:
        for start_z in (120.0, 60.0, 180.0):      # through, beside and past the shapes
            floor, extra = bc.scenario(bg, kind)
            statics = [floor] + [b for b in extra if not b.get('is_mover')]
            movers = [b for b in extra if b.get('is_mover')]
            grid = SpatialGrid()
            grid.populate(statics + movers)
            homes = [list(m['pos']) for m in movers]
            player = Player(-760.0, start_z, angle=0.0)
            player.pos.y = 30.0
            move = glm.vec3(1.0, 0.0, 0.0)
            ticks = []
            for tick in range(960):
                for i, m in enumerate(movers):
                    y = 40.0 + 20.0 * math.sin(tick * 0.05 + i)
                    angle = (30.0 + tick * 2.0) % 360.0 if m.get('rotate') else 0.0
                    h = homes[i]
                    if source is not None:
                        source.set(m, centre=(h[0], y, h[2]),
                                   spin=((0.0, 1.0, 0.0), angle) if angle else None)
                    else:
                        m['pos'][1] = y
                        if angle:
                            m['_rot_angle'] = angle
                player.update(1.0 / 60.0, move, tick % 37 == 0, False, statics,
                              movers=movers, spatial_grid=grid)
                if player.pos.x > 900:
                    player.pos.x = -760.0
                g = player.ground_object
                ticks.append([player.pos.x, player.pos.y, player.pos.z, player.velocity.x,
                              player.velocity.y, player.velocity.z, bool(player.on_ground),
                              g.get('name') if isinstance(g, dict) else None])
            res['%s@%g' % (kind, start_z)] = ticks
            if source is not None:
                source.clear()
    with open(out, 'w') as fh:
        json.dump(res, fh)


if __name__ == '__main__':
    main()
