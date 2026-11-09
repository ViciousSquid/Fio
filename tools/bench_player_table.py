"""The player beside movers driven by the game's own mover table, on any
checkout: the production path, end to end.

    python tools/bench_player_table.py TREE [--kind box_movers] [--calls N]
    python tools/bench_player_table.py TREE --kind all
    python tools/bench_player_table.py TREE --trace OUT.json

``bench_player_movers.py`` and the gate's ``player`` probe drive Stage 2's
movers through a ``RuntimeOverrides`` (eight stamps a tick, the source's own
update not timed) and Stage 1's by writing ``pos``. Neither is how a level
moves them: the logic thread ticks the ``MoverTable`` (Stage 1: which writes
``pos`` into each brush; Stage 2: which publishes runtime transforms, once a
tick), then updates the player with every mover and door in the level. This
does exactly that, with each tree's own table and player, so the publication
and the reader are measured together.

*TREE* is a checkout. The scenarios come from this file; the engine from
*TREE*.

Scenarios (``--kind``): the player walks along a row of eight shapes on a
floor, as ``bench_collision`` lays them out, but each is a linear mover
travelling up and down (``box_movers``, ``angled_movers``,
``spinning_boxes``, ``spinning_angled``); ``far_doors``: 34 doors opening and
closing away from the player (BigWorld has 34 doors); ``one_mover``: a single
mover beside the path (what most shipped maps have); ``all`` runs each.

Reports, over 900 ticks after 60 of warm-up, per tick:

* ``tick``: the mover table's tick (``_update_movers`` + ``_update_doors``),
  which includes Stage 2's publication to the resolver;
* ``player``: ``Player.update`` with the level's mover and door lists;
* ``total``: the two together;
* ``calls``: Python calls (cProfile) in the tick and in the player update.

Timings are per-process medians (and minima): run trees interleaved,
pinned to one core (``taskset -c 3``), several times, and compare.

``--trace OUT.json`` instead writes the player's state every tick through
every scenario from three starting lanes, jumping and ducking now and then:
two Stage 2 trees that collide alike write byte-identical files (``cmp``).
"""
import argparse
import cProfile
import gc
import json
import math
import os
import pstats
import statistics
import sys
import time

KINDS = ('box_movers', 'angled_movers', 'spinning_boxes', 'spinning_angled',
         'far_doors', 'one_mover')


def _box(name, pos, size, **extra):
    brush = {'id': name, 'name': name, 'pos': [float(v) for v in pos],
             'size': [float(v) for v in size], 'textures': {}}
    brush.update(extra)
    return brush


def _angled(bg, name, pos, size, **extra):
    brush = _box(name, pos, size, **extra)
    bg.box_to_geometry(brush)
    bg.clip_brush(brush, [0.35, 1.0, 0.0], float(pos[1]) * 1.0 + 4.0)
    return brush


def _mover(i, **extra):
    """A linear mover travelling up and down from its authored centre."""
    out = {'is_mover': True, 'start_on': True, 'direction': [0, 1, 0],
           'distance': 40.0, 'speed': 30.0 + 3.0 * i}
    out.update(extra)
    return out


def scenario(bg, kind):
    """``(brushes, statics)``: the level, and the brushes the static grid
    and the player's ``brushes`` argument get."""
    floor = _box('floor', (0, -20, 0), (4000, 40, 4000))
    extra = []
    if kind == 'far_doors':
        for i in range(34):
            extra.append(_box('d%d' % i, (-1600 + i * 96, 64, 1600), (64, 128, 16),
                              is_door=True, door_speed=96.0 + i, door_distance=96.0,
                              door_direction='up', open_time=0.25))
    elif kind == 'one_mover':
        extra.append(_box('m0', (-300, 20, 120), (96, 32, 96), **_mover(0)))
    else:
        for i in range(8):
            x = -700 + i * 200
            if kind == 'box_movers':
                extra.append(_box('m%d' % i, (x, 20, 120), (96, 32, 96), **_mover(i)))
            elif kind == 'angled_movers':
                extra.append(_angled(bg, 'am%d' % i, (x, 20, 120), (96, 64, 96),
                                     **_mover(i)))
            elif kind == 'spinning_boxes':
                extra.append(_box('s%d' % i, (x, 20, 120), (160, 16, 16),
                                  **_mover(i, rotate=True, rot_axis=[0, 1, 0])))
            elif kind == 'spinning_angled':
                extra.append(_angled(bg, 'sa%d' % i, (x, 20, 120), (96, 64, 96),
                                     **_mover(i, rotate=True, rot_axis=[0, 1, 0])))
    brushes = [floor] + extra
    statics = [b for b in brushes if not (b.get('is_mover') or b.get('is_door'))]
    return brushes, statics


class World:
    """One level in play: a logic thread's mover runtime, a grid, a player."""

    def __init__(self, kind, start_z=120.0):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5.QtWidgets import QApplication
        self._app = QApplication.instance() or QApplication([])
        import glm
        from editor.editor_state import EditorState
        from engine import brush_geometry as bg
        from engine.logic_thread import LogicThread
        from engine.physics import SpatialGrid
        from engine.player import Player
        from engine.threaded_game_state import ThreadedGameState
        self.glm = glm
        state = EditorState()
        state.brushes, self.statics = scenario(bg, kind)
        logic = LogicThread(ThreadedGameState(), state)
        gc.collect()
        self.player = Player(-760.0, start_z, angle=0.0)
        self.player.pos.y = 30.0
        logic.player_runtime.player = self.player
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        logic.io_manager = None
        self.runtime = logic.mover_runtime
        self.runtime._init_movers()
        self.runtime._init_doors()
        self.grid = SpatialGrid()
        self.grid.populate(state.brushes)
        self.logic = logic
        self.move = glm.vec3(1.0, 0.0, 0.0)

    def tick(self, delta=1.0 / 60.0):
        runtime = self.runtime
        for s in runtime.door_states.values():
            if s['state'] == 'closed':
                s['state'] = 'opening'
        runtime._update_movers(delta)
        runtime._update_doors(delta)

    def update(self, delta=1.0 / 60.0, jump=False, crouch=False):
        runtime = self.runtime
        self.player.update(delta, self.move, jump, crouch, self.statics,
                           runtime._mover_brush_list, runtime._door_brush_list,
                           spatial_grid=self.grid)
        if self.player.pos.x > 900:
            self.player.pos.x = -760.0

    def close(self):
        """Stop play as the game does (every mover and door home, the
        mover table's entries dropped), so the next world is the only one
        a runtime source answers for, and let this one go."""
        self.runtime._reset_movers()
        self.runtime._reset_doors()
        self.logic.stop()
        self.logic = self.runtime = self.player = self.grid = None
        gc.collect()


def measure(kind, calls):
    world = World(kind)
    for _ in range(60):
        world.tick()
        world.update()
    # Calls per tick, deterministic.
    prof_tick, prof_player = cProfile.Profile(), cProfile.Profile()
    for _ in range(900):
        prof_tick.enable()
        world.tick()
        prof_tick.disable()
        prof_player.enable()
        world.update()
        prof_player.disable()
    tick_calls = sum(v[1] for v in pstats.Stats(prof_tick).stats.values()) / 900.0
    player_calls = sum(v[1] for v in pstats.Stats(prof_player).stats.values()) / 900.0
    ticks, players = [], []
    now = time.perf_counter
    for _ in range(900):
        t0 = now()
        world.tick()
        t1 = now()
        world.update()
        t2 = now()
        ticks.append((t1 - t0) * 1e6)
        players.append((t2 - t1) * 1e6)
    world.close()
    totals = [a + b for a, b in zip(ticks, players)]
    out = {'tick': ticks, 'player': players, 'total': totals,
           'tick_calls': tick_calls, 'player_calls': player_calls}
    if calls:
        out['top'] = prof_player
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tree')
    ap.add_argument('--kind', default='box_movers')
    ap.add_argument('--calls', type=int, default=0)
    ap.add_argument('--trace')
    ap.add_argument('--json')
    args = ap.parse_args()
    tree = os.path.abspath(args.tree)
    os.chdir(tree)
    sys.path.insert(0, tree)
    if args.trace:
        trace(args.trace)
        return
    kinds = KINDS if args.kind == 'all' else (args.kind,)
    name = os.path.basename(tree.rstrip('/'))
    results = {}
    for kind in kinds:
        r = measure(kind, args.calls)
        med = statistics.median
        print('%s %-16s calls tick %6.1f player %6.1f | tick %6.1f/%6.1f  player %6.1f/%6.1f  '
              'total %6.1f/%6.1f  (us, min/median)' % (
                  name, kind, r['tick_calls'], r['player_calls'],
                  min(r['tick']), med(r['tick']), min(r['player']), med(r['player']),
                  min(r['total']), med(r['total'])))
        if args.calls:
            import io
            buf = io.StringIO()
            pstats.Stats(r.pop('top'), stream=buf).sort_stats('calls').print_stats(args.calls)
            for line in buf.getvalue().splitlines():
                if '/' in line or '{' in line:
                    print('   ' + line.replace(tree + '/', '')[:140])
        results[kind] = {k: v for k, v in r.items() if k != 'top'}
    if args.json:
        with open(args.json, 'w') as fh:
            json.dump(results, fh)


def trace(out):
    """The player's state every tick, through every scenario from three
    lanes (through, beside and past the shapes), jumping and ducking now and
    then -- including a long fall onto the movers, dashes (one of a hundred
    units a tick) and the player put inside the nearest mover (pushed out
    a long way), so the rare paths run too."""
    from engine import brush_placement
    res = {}
    for kind in KINDS:
        for start_z in (120.0, 60.0, 180.0):
            world = World(kind, start_z)
            player = world.player
            runtime = world.runtime
            ticks = []
            for tick in range(960):
                world.tick()
                if tick % 240 == 100:
                    # A drop from high above the row, onto whatever is there.
                    player.pos.y = 900.0
                if tick % 300 == 150:
                    # A dash: far more than a tick's walk.
                    player.velocity.x = 2400.0
                if tick % 300 == 250:
                    player.velocity.x = 6000.0
                if tick % 160 == 60:
                    # Inside the nearest mover or door, where it is now.
                    shapes = runtime._mover_brush_list + runtime._door_brush_list
                    if shapes:
                        centres = [brush_placement.world_box(b)[:3] for b in shapes]
                        best = min(centres, key=lambda c: abs(c[0] - player.pos.x))
                        player.pos.x, player.pos.y, player.pos.z = best
                world.update(jump=tick % 37 == 0, crouch=(tick // 50) % 5 == 3)
                g = player.ground_object
                ticks.append([player.pos.x, player.pos.y, player.pos.z, player.velocity.x,
                              player.velocity.y, player.velocity.z, bool(player.on_ground),
                              bool(player.ducked),
                              g.get('name') if isinstance(g, dict) else None])
            world.close()
            res['%s@%g' % (kind, start_z)] = ticks
    with open(out, 'w') as fh:
        json.dump(res, fh)


if __name__ == '__main__':
    main()
