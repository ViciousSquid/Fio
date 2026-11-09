"""Performance probes for the Stage 1 / Stage 2 gate (tools/perf_gate.py).

    python tools/perf_probes.py PROBE --tree PATH --out samples.json

Each probe measures one family of workloads on the checkout at ``--tree``
and writes ``{metric: [samples...]}`` (microseconds unless the metric name
says ``_ms``). The probes only use surface both trees have, and drive movers
the way each tree does: through a runtime source where the tree has one
(Stage 2), by writing ``pos`` / ``_rot_angle`` before that -- so the same
workload is measured on both.

Probes: movers, aabb, static, ai, player, table, frames and gameplay (the
last two need GL: xvfb-run).
"""

import argparse
import gc
import json
import math
import os
import sys
import time

PROBES = ('movers', 'aabb', 'static', 'ai', 'player', 'table', 'frames', 'gameplay')


def _now():
    return time.perf_counter()


def _us(t0, t1, n=1):
    return (t1 - t0) * 1e6 / n


def _qt():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _maps(name):
    with open(os.path.join('maps', name)) as handle:
        return json.load(handle)['brushes']


def _copy(brushes):
    return json.loads(json.dumps(brushes))


def _runtime_source():
    try:
        from engine import brush_placement as bp
        source = bp.RuntimeOverrides()
        bp.register_runtime(source)
        return source
    except (ImportError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Movers: tick, publication, the readers that follow them
# ---------------------------------------------------------------------------

def _mover_world(count):
    out = []
    for k in range(count):
        out.append({"id": "m%d" % k, "name": "m%d" % k, "pos": [k * 150.0, 0.0, 0.0],
                    "size": [64.0, 64.0, 64.0], "is_mover": True, "start_on": True,
                    "speed": 64.0 + k, "distance": 128.0, "direction": [0, 1, 0],
                    "rotate": k % 3 == 0})
    for k in range(count):
        out.append({"id": "d%d" % k, "name": "d%d" % k, "pos": [k * 150.0, 0.0, 400.0],
                    "size": [64.0, 64.0, 64.0], "is_door": True,
                    "door_speed": 128.0 + k, "door_distance": 96.0,
                    "door_direction": "up", "open_time": 0.25})
    # Static scenery around them, as a level has.
    for k in range(count * 8):
        out.append({"id": "s%d" % k, "name": "s%d" % k,
                    "pos": [(k % 40) * 90.0, -40.0, (k // 40) * 90.0 - 200.0],
                    "size": [80.0, 16.0, 80.0]})
    return out


def probe_movers(out, ticks=300):
    _qt()
    from editor.editor_state import EditorState
    from engine.constants import brush_aabb_bounds
    from engine.logic_collision import CollisionRows
    from engine.logic_thread import LogicThread
    from engine.physics import SpatialGrid
    from engine.player import Player
    from engine.render_table import RenderTable
    from engine.threaded_game_state import ThreadedGameState
    delta = 1.0 / 60.0
    for count in (8, 32, 128):
        state = EditorState()
        state.brushes = _mover_world(count)
        logic = LogicThread(ThreadedGameState(), state)
        # The plugin manager keeps the last logic thread it was handed until a
        # new one replaces it: only now is the previous count's world garbage.
        gc.collect()
        logic.player_runtime.player = Player(1.0, 3.0)
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        logic.io_manager = None
        runtime = logic.mover_runtime
        runtime._init_movers()
        runtime._init_doors()
        table = RenderTable()
        table.sync(state.brushes, 1)
        rows = CollisionRows(state.brushes, 1)
        grid = SpatialGrid()
        grid.populate(state.brushes)
        cells = grid._cell_rows
        mover = state.brushes[1]
        series = {k: [] for k in ('tick', 'publish', 'rows_first', 'rows_again',
                                  'cells_first', 'cells_again', 'aabb_moving',
                                  'frame_total')}
        for i in range(ticks + 40):
            for s in runtime.door_states.values():
                if s["state"] == "closed":
                    s["state"] = "opening"
            runtime._mover_table.sync()
            t0 = _now()
            runtime._update_movers(delta)
            runtime._update_doors(delta)
            t1 = _now()
            runtime._mover_table.publish(logic, table)
            t2 = _now()
            rows.refresh_movers()
            t3 = _now()
            rows.refresh_movers()
            t4 = _now()
            cells.refresh_movers()
            t5 = _now()
            cells.refresh_movers()
            t6 = _now()
            for _ in range(20):
                brush_aabb_bounds(mover)
            t7 = _now()
            if i >= 40:
                series['tick'].append(_us(t0, t1))
                series['publish'].append(_us(t1, t2))
                series['rows_first'].append(_us(t2, t3))
                series['rows_again'].append(_us(t3, t4))
                series['cells_first'].append(_us(t4, t5))
                series['cells_again'].append(_us(t5, t6))
                series['aabb_moving'].append(_us(t6, t7, 20))
                series['frame_total'].append(_us(t0, t7))
        for key, values in series.items():
            out['movers%d_%s' % (count, key)] = values
        logic.stop()
        # One world at a time, as the game has: let this one go before the
        # next count (a stopped logic thread's tables are otherwise kept
        # until a cyclic collection, and a resolver would still ask them).
        del logic, runtime, table, rows, grid, cells, mover, state
        gc.collect()


# ---------------------------------------------------------------------------
# The AABB cache on static brushes
# ---------------------------------------------------------------------------

def probe_aabb(out, batches=200, per=2000):
    from engine import brush_geometry as bg
    from engine.constants import brush_aabb_bounds
    show = _copy(_maps('_SHOWCASE.json'))
    box = {'pos': [1.0, 2.0, 3.0], 'size': [64.0, 64.0, 64.0], 'name': 'b',
           'textures': {}, 'color': [1, 1, 1]}
    cut = next(b for b in show if 'geometry' in b)
    if hasattr(bg, 'local_convex'):
        # Stage 2d stores local planes: hand it the brush as its loader would.
        from engine import brush_placement as bp
        bp.localise_legacy(cut)
    bg.get_convex(cut)
    brush_aabb_bounds(box)
    brush_aabb_bounds(cut)
    for label, brush in (('plain_hit', box), ('angled_hit', cut)):
        values = []
        for _ in range(batches):
            t0 = _now()
            for _ in range(per):
                brush_aabb_bounds(brush)
            values.append(_us(t0, _now(), per))
        out['aabb_' + label] = values
    values = []
    k = 0
    for _ in range(batches):
        t0 = _now()
        for _ in range(per // 4):
            k += 1
            box['pos'] = [float(k), 2.0, 3.0]
            brush_aabb_bounds(box)
        values.append(_us(t0, _now(), per // 4))
    out['aabb_plain_miss'] = values


# ---------------------------------------------------------------------------
# Static processing: tables, grid, collision rows, Big World
# ---------------------------------------------------------------------------

def probe_static(out, reps=15):
    from engine.logic_collision import CollisionRows
    from engine.physics import SpatialGrid
    from engine.render_table import RenderTable
    from plugins.bigworld.manager import BigWorldManager
    for label, name in (('big', 'BigWorld_streaming_test.json'), ('show', '_SHOWCASE.json')):
        raw = _copy(_maps(name))
        if any('geometry' in b for b in raw):
            try:
                from engine import brush_placement as bp
                for b in raw:
                    if 'geometry' in b and hasattr(bp, 'localise_legacy'):
                        bp.localise_legacy(b)
            except ImportError:
                pass
        RenderTable().sync(raw, 1)
        SpatialGrid().populate(raw)
        for key, fn in (('rt_sync', lambda: RenderTable().sync(raw, 1)),
                        ('grid_populate', lambda: SpatialGrid().populate(raw)),
                        ('collision_rows', lambda: CollisionRows(raw, 1)),
                        ('bigworld_index', lambda: BigWorldManager().index_world(raw, []))):
            values = []
            for _ in range(reps):
                t0 = _now()
                fn()
                values.append((_now() - t0) * 1e3)
            out['static_%s_%s_ms' % (label, key)] = values


# ---------------------------------------------------------------------------
# AI queries against the static world
# ---------------------------------------------------------------------------

def probe_ai(out, batches=60):
    import numpy as np
    from engine.physics import SpatialGrid
    for label, name in (('big', 'BigWorld_streaming_test.json'), ('show', '_SHOWCASE.json')):
        raw = _copy(_maps(name))
        try:
            from engine import brush_placement as bp
            for b in raw:
                if 'geometry' in b and hasattr(bp, 'localise_legacy'):
                    bp.localise_legacy(b)
        except ImportError:
            pass
        grid = SpatialGrid()
        grid.populate(raw)
        xs = [b['pos'][0] for b in raw]
        zs = [b['pos'][2] for b in raw]
        lo_x, hi_x, lo_z, hi_z = min(xs), max(xs), min(zs), max(zs)
        pts = [(lo_x + (hi_x - lo_x) * ((i * 37) % 1000) / 1000.0, 64.0,
                lo_z + (hi_z - lo_z) * ((i * 91) % 1000) / 1000.0) for i in range(4000)]
        values = []
        for b in range(batches):
            chunk = pts[(b * 200) % 4000:(b * 200) % 4000 + 200]
            t0 = _now()
            for x, y, z in chunk:
                grid.overlaps_wall(x, y, z, 16.0)
            values.append(_us(t0, _now(), len(chunk)))
        out['ai_%s_overlaps_wall' % label] = values
        values = []
        for b in range(batches):
            chunk = pts[(b * 200) % 4000:(b * 200) % 4000 + 200]
            t0 = _now()
            for x, y, z in chunk:
                grid.raycast_down(x, z, 500.0)
            values.append(_us(t0, _now(), len(chunk)))
        out['ai_%s_raycast_down' % label] = values
        mx = np.array([p[0] for p in pts[:32]])
        my = np.array([p[1] for p in pts[:32]])
        mz = np.array([p[2] for p in pts[:32]])
        values = []
        for _ in range(batches):
            t0 = _now()
            grid.overlaps_wall_batch(mx, my, mz, 16.0)
            grid.raycast_down_batch(mx, mz, my + 10.0)
            values.append(_us(t0, _now()))
        out['ai_%s_batch32' % label] = values
        if hasattr(grid, 'has_line_of_sight'):
            import glm
            values = []
            for b in range(batches):
                chunk = pts[(b * 50) % 4000:(b * 50) % 4000 + 50]
                t0 = _now()
                for (x, y, z), (x2, y2, z2) in zip(chunk, chunk[1:]):
                    grid.has_line_of_sight(glm.vec3(x, y, z), glm.vec3(x2, y2, z2))
                values.append(_us(t0, _now(), max(1, len(chunk) - 1)))
            out['ai_%s_line_of_sight' % label] = values


# ---------------------------------------------------------------------------
# Player collision (tools/bench_collision.py's scenarios)
# ---------------------------------------------------------------------------

def probe_player(out, ticks=900):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import bench_collision as bc
    import glm
    from engine import brush_geometry as bg
    from engine.physics import SpatialGrid
    from engine.player import Player
    source = _runtime_source()
    for kind in bc.SCENARIOS:
        floor, extra = bc.scenario(bg, kind)
        statics = [floor] + [b for b in extra if not b.get('is_mover')]
        movers = [b for b in extra if b.get('is_mover')]
        grid = SpatialGrid()
        grid.populate(statics + movers)
        homes = [list(m['pos']) for m in movers]
        player = Player(-760.0, 120.0, angle=0.0)
        player.pos.y = 30.0
        move = glm.vec3(1.0, 0.0, 0.0)
        values = []
        for tick in range(ticks + 60):
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
            t0 = _now()
            player.update(1.0 / 60.0, move, False, False, statics, movers=movers,
                          spatial_grid=grid)
            t1 = _now()
            if player.pos.x > 900:
                player.pos.x = -760.0
            if tick >= 60:
                values.append(_us(t0, t1))
        out['player_' + kind] = values
        if source is not None:
            source.clear()


# ---------------------------------------------------------------------------
# The player beside movers driven by the mover table (the production path)
# ---------------------------------------------------------------------------

def probe_table(out):
    """tools/bench_player_table.py's scenarios: each tree's own mover table
    ticked as the logic thread ticks it (Stage 2's publication included),
    then the player updated with the level's mover and door lists -- the
    tick, the player, both, and the player's Python calls a tick (exact)."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import bench_player_table as bt
    for kind in bt.KINDS:
        result = bt.measure(kind, False)
        for key in ('tick', 'player', 'total'):
            out['table_%s_%s' % (kind, key)] = result[key]
        out['table_%s_player_calls' % kind] = [result['player_calls']]
        gc.collect()


# ---------------------------------------------------------------------------
# Frames (GL)
# ---------------------------------------------------------------------------

def probe_frames(out, frames=150):
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))
    import numpy as np
    import OpenGL.GL as gl
    import bench_geo_passes as passes
    from bench_geo_edit import _camera
    from engine.constants import RENDER_MODE_LIT
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    from engine.renderer.forward import ForwardRenderer
    keep = passes._context(640, 360)
    source = _runtime_source()
    for label, name in (('big', 'BigWorld_streaming_test.json'), ('show', '_SHOWCASE.json')):
        renderer = ForwardRenderer(None)
        renderer.shadows_enabled = False
        renderer.set_grid(4096, 64)
        renderer.set_sprite_textures({})
        brushes = _copy(_maps(name))
        try:
            from engine import brush_placement as bp
            for b in brushes:
                if 'geometry' in b and hasattr(bp, 'localise_legacy'):
                    bp.localise_legacy(b)
        except ImportError:
            pass
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
        camera = _camera(brushes, 640, 360)
        boxes = [i for i, b in enumerate(brushes) if not b.get('geometry')][:32]
        for i in boxes:
            brushes[i]['is_mover'] = True
        homes = {i: list(brushes[i]['pos']) for i in boxes}

        def move(k):
            for i in boxes:
                h = homes[i]
                y = h[1] + 8.0 * ((k + i) % 7)
                if source is not None:
                    source.set(brushes[i], centre=(h[0], y, h[2]))
                else:
                    brushes[i]['pos'] = [h[0], y, h[2]]
            table.refresh_transforms(brushes, np.asarray(boxes, dtype=np.intp))

        for scenario in ('still', 'moving'):
            values = []
            for k in range(frames + 15):
                if scenario == 'moving':
                    move(k)
                t0 = _now()
                renderer.render_scene(*camera, None, config,
                                      brush_slots=config["all_brush_slots"])
                t1 = _now()
                gl.glFinish()
                if k >= 15:
                    values.append((t1 - t0) * 1e3)
            out['frame_%s_%s_ms' % (label, scenario)] = values
        if source is not None:
            source.clear()
        renderer.cleanup()
    del keep


# ---------------------------------------------------------------------------
# Representative gameplay: the real editor host (GL)
# ---------------------------------------------------------------------------

GAMEPLAY_MAPS = (('show', '_SHOWCASE.json'), ('monster', 'MonsterTest.json'),
                 ('big', 'BigWorld_streaming_test.json'), ('office', 'Office_Corridor.json'))


def probe_gameplay(out, frames=240):
    """tools/bench_compare.py's production path -- MainWindow.load_level_file,
    enter_play_mode, the logic thread stepped by hand one fixed tick a frame
    with its scripted input, the monster AI run on this thread every other
    frame, QtGameView.paintGL -- keeping every frame's sample: logic (step and
    publish), AI, paint and the frame (logic + paint), in ms, and the Python
    calls a logic frame makes (a count the machine does not change)."""
    import cProfile
    import pstats
    import threading
    os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
    sys.path.insert(0, os.path.join(os.getcwd(), 'tools'))
    from PyQt5.QtWidgets import QApplication
    # The editor host records recent files in settings.ini: put it back.
    settings = os.path.join(os.getcwd(), 'settings.ini')
    saved = open(settings, 'rb').read() if os.path.exists(settings) else None
    import atexit

    def restore():
        if saved is not None:
            with open(settings, 'wb') as handle:
                handle.write(saved)
    atexit.register(restore)
    app = QApplication.instance() or QApplication([])
    import bench_compare as bc
    from OpenGL import GL
    from PyQt5.QtCore import Qt
    bench = bc.Bench(app)
    for label, name in GAMEPLAY_MAPS:
        window, view = bench.window, bench.view
        gc.collect()
        logic = bench._halt_logic_thread()
        window.load_level_file(os.path.join(os.getcwd(), 'maps', name))
        app.processEvents()
        window.enter_play_mode()
        for thread in threading.enumerate():
            if thread.name == "MonsterAIThread":
                thread.stop()
                thread.join(5.0)
        monster_ai = logic.monster_ai
        game_state = view.game_state
        tick = logic.TICK_DURATION
        series = {k: [] for k in ('logic', 'ai', 'paint', 'frame')}
        profile = cProfile.Profile()

        def frame(index, measure, profiled=False):
            keys, dx, dy, fire = bc._script(index, Qt.Key_W, Qt.Key_A)
            game_state.set_keys(keys)
            game_state.set_mouse_delta(dx, dy)
            if fire:
                game_state.queue_shot()
            t0 = _now()
            if profiled:
                profile.enable()
            logic._step_frame(tick)
            if profiled:
                profile.disable()
            ta = _now()
            if index % 2 == 0:
                monster_ai.update(2.0 * tick)
            tb = _now()
            if profiled:
                profile.enable()
            logic._publish_frame()
            if profiled:
                profile.disable()
            t1 = _now()
            if game_state.try_swap():
                view.makeCurrent()
                view.paintGL()
                GL.glFinish()
                view.doneCurrent()
            t2 = _now()
            if measure:
                series['logic'].append((t1 - t0 - (tb - ta)) * 1e3)
                if index % 2 == 0:
                    series['ai'].append((tb - ta) * 1e3)
                series['paint'].append((t2 - t1) * 1e3)
                series['frame'].append((t2 - t0 - (tb - ta)) * 1e3)

        for index in range(30):
            frame(index, False)
        gc.collect()
        for index in range(30, 30 + frames):
            frame(index, True)
        # The call count: a further 60 frames under the profiler (timings of
        # those frames are not kept).
        for index in range(30 + frames, 90 + frames):
            frame(index, False, profiled=True)
        calls = sum(entry[1] for entry in pstats.Stats(profile).stats.values()) / 60.0
        window.enter_play_mode()   # Stop
        app.processEvents()
        for key, values in series.items():
            out['gameplay_%s_%s_ms' % (label, key)] = values
        out['gameplay_%s_logic_calls' % label] = [calls]
    restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('probe', choices=PROBES)
    parser.add_argument('--tree', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    out_path = os.path.abspath(args.out)
    tree = os.path.abspath(args.tree)
    os.chdir(tree)
    sys.path.insert(0, tree)
    out = {}
    globals()['probe_' + args.probe](out)
    with open(out_path, 'w') as handle:
        json.dump(out, handle)


if __name__ == '__main__':
    main()
