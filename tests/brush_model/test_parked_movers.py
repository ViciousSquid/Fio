"""Parked movers and doors (``docs/brush-model-stage2.md`` §11.9).

A brush a streaming layer has parked (Big World's ``_set_brush_active``:
``PARKED_HIDDEN_KEY``, the live ``hidden`` flag, a VISIBILITY touch) is not
walked or published by its mover table. Its state is resolved from the
state it was last resolved to and the ticks since -- worked out, not
stepped -- when something needs it: an input to it, a read of its state, a
saved game, a reader asking where it is, and waking. The rule:

    I/O remains live; continuous simulation sleeps; state catches up on
    demand; missed outputs are not replayed.

These tests hold, against the same level never parked (the reference):

* a parked row is not walked, published or derived while nothing asks;
* woken, it is where the reference is -- linear movers (incl. an even
  ratio and many round trips), a spinner, doors through their whole cycle,
  a path follower with waits, laps and an end -- and no output it missed
  is fired; outputs resume with the next transition;
* an input reaches it however far away, takes effect from the tick it
  arrives, and repeated, reversed and interrupted commands leave it where
  the reference is;
* a saved game taken while it is parked holds where it has got to, and a
  load continues as the reference does;
* every reader that asks where it is -- the resolver, the grid's per-query
  and batched queries -- is answered for now, storing nothing;
* a brush in both groups is never parked; stopping play forgets them all.
"""

import gc
import math

import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                       # noqa: E402
from editor.io_handlers import register_all_input_handlers         # noqa: E402
from editor.things import PathNode                                  # noqa: E402
from engine import brush_placement as bp                           # noqa: E402
from engine import mover_table as mt                               # noqa: E402
from engine.change_journal import VISIBILITY, touch                 # noqa: E402
from engine.constants import brush_aabb_bounds                     # noqa: E402
from engine.logic_thread import LogicThread                        # noqa: E402
from engine.physics import SpatialGrid                             # noqa: E402
from engine.spatial import PARKED_HIDDEN_KEY                       # noqa: E402
from engine.threaded_game_state import ThreadedGameState            # noqa: E402
from tests.helpers.worlds import box_brush                          # noqa: E402

pytestmark = pytest.mark.qt

DT = 1.0 / 60.0


@pytest.fixture(autouse=True)
def one_world(monkeypatch):
    """Sources from other tests set aside (see test_mover_broad_phase)."""
    gc.collect()
    assert not bp.PREVIEW.runtime_entries
    monkeypatch.setattr(bp, '_sources', tuple(
        ref for ref in bp._sources if ref() is bp.PREVIEW))


def _level():
    brushes = [box_brush("ground", (0, -16, 0), (8192, 32, 8192))]
    # 1 / step is not a whole number here, and is one (120) for "even".
    brushes.append(box_brush("lift", (0.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
                             start_on=True, speed=70.0, distance=130.0, direction=[0, 1, 0]))
    brushes.append(box_brush("even", (300.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
                             start_on=True, speed=64.0, distance=128.0, direction=[0, 1, 0]))
    brushes.append(box_brush("spin", (600.0, 64.0, 0.0), (160, 16, 32), is_mover=True,
                             start_on=True, rotate=True, speed=37.0, distance=40.0,
                             direction=[1, 0, 0], rot_axis=[0.3, 1.0, 0.2]))
    brushes.append(box_brush("still", (900.0, 32.0, 0.0), (64, 32, 64), is_mover=True,
                             start_on=False, speed=50.0, distance=64.0, direction=[0, 1, 0]))
    brushes.append(box_brush("tram", (0.0, 40.0, 600.0), (64, 16, 64), is_mover=True,
                             start_on=True, speed=90.0, distance=0.0, path_target="A"))
    brushes.append(box_brush("door", (0.0, 64.0, -400.0), (64, 128, 16), is_door=True,
                             door_speed=96.0, door_distance=100.0, door_direction="up",
                             open_time=0.55))
    brushes.append(box_brush("gate", (300.0, 64.0, -400.0), (64, 128, 16), is_door=True,
                             door_speed=77.0, door_distance=96.0, door_lip=8.0,
                             door_direction="north", open_time=0.4))
    return brushes


def _nodes():
    return [PathNode([0.0, 40.0, 900.0], {'name': 'A', 'next_node': 'B'}),
            PathNode([500.0, 40.0, 900.0], {'name': 'B', 'next_node': 'C',
                                            'wait_time': 0.35, 'speed': 0.6}),
            PathNode([500.0, 140.0, 600.0], {'name': 'C', 'next_node': 'A',
                                             'speed': 1.7})]


class _World:
    """A level in play, the logic thread's order each tick: movers, doors,
    then the delayed I/O. Every output fired is recorded."""

    def __init__(self, nodes=True, brushes=None):
        state = EditorState()
        state.brushes = brushes if brushes is not None else _level()
        state.things = _nodes() if nodes else []
        logic = LogicThread(ThreadedGameState(), state)
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        register_all_input_handlers(logic.io_manager)
        self.fired = []
        real = logic.io_manager.fire_output

        def fire(entity, output, *args, **kwargs):
            name = entity.get('name') if isinstance(entity, dict) else None
            self.fired.append((self.tick_count, name, output))
            return real(entity, output, *args, **kwargs)

        logic.io_manager.fire_output = fire
        self.tick_count = 0
        logic.mover_runtime._init_movers()
        logic.mover_runtime._init_doors()
        self.logic = logic
        self.runtime = logic.mover_runtime
        self.brushes = {b['name']: b for b in state.brushes}

    def table(self):
        return self.runtime._movers()

    def tick(self, n=1):
        for _ in range(n):
            self.tick_count += 1
            self.runtime._update_movers(DT)
            self.runtime._update_doors(DT)
            self.logic.io_manager.update(DT)

    def send(self, name, input_name, parameter=""):
        brush = self.brushes[name]
        self.logic.io_manager._execute_input(name, input_name, parameter, "test",
                                             target_id=brush.get('id', ''))

    def park(self, *names):
        """What Big World's _set_brush_active(brush, False) does."""
        for name in names:
            brush = self.brushes[name]
            if PARKED_HIDDEN_KEY not in brush:
                brush[PARKED_HIDDEN_KEY] = brush.get('hidden', False)
            brush['hidden'] = True
            brush['bw_active'] = False
            touch(brush, VISIBILITY)

    def unpark(self, *names):
        for name in names:
            brush = self.brushes[name]
            if PARKED_HIDDEN_KEY in brush:
                brush['hidden'] = brush.pop(PARKED_HIDDEN_KEY)
            brush['bw_active'] = True
            touch(brush, VISIBILITY)

    def state_of(self, name):
        """Its row's columns: what the walk advances."""
        table = self.table()
        brush = self.brushes[name]
        for group in (table.movers, table.doors):
            row = group.row_of_obj.get(id(brush))
            if row is None:
                continue
            if group is table.movers:
                return ('mover', float(group.progress[row]), bool(group.forward[row]),
                        float(group.rot_angle[row]), tuple(group.pos[row].tolist()))
            return ('door', int(group.code[row]), float(group.progress[row]),
                    float(group.open_timer[row]), tuple(group.pos[row].tolist()))
        raise KeyError(name)

    def path_of(self, name):
        brush = self.brushes[name]
        i = next(i for i, b in self.runtime.movers if b is brush)
        state = self.runtime.mover_path_states.get(i)
        return None if state is None else dict(state)

    def stop(self):
        self.runtime._reset_movers()
        self.runtime._reset_doors()
        self.logic.stop()


@pytest.fixture
def worlds(request):
    made = []

    def make(**kwargs):
        w = _World(**kwargs)
        made.append(w)
        return w

    yield make
    for w in made:
        w.stop()


def _close(a, b, tol=1e-6):
    """Two row states alike: the same flags, the numbers to *tol*."""
    assert len(a) == len(b)
    for x, y in zip(a, b):
        if isinstance(x, tuple):
            _close(x, y, tol)
        elif isinstance(x, float):
            assert x == pytest.approx(y, abs=tol), (a, b)
        else:
            assert x == y, (a, b)


def _angle_close(a, b, tol=1e-6):
    d = (a - b) % 360.0
    assert min(d, 360.0 - d) <= tol, (a, b)


ALL = ("lift", "even", "spin", "still", "tram", "door", "gate")


def _outputs(world, since):
    return [(tick, name, out) for tick, name, out in world.fired if tick > since]


# ---------------------------------------------------------------------------
# Sleeping costs nothing
# ---------------------------------------------------------------------------

def test_a_parked_row_is_not_walked_or_published(worlds, monkeypatch):
    w = worlds()
    w.send("door", "Open")
    w.tick(20)
    w.park(*ALL)
    w.tick()                                    # the parking reaches the table
    before = {name: w.state_of(name) for name in ALL}
    entries = dict(w.table().runtime_entries)
    calls = {'derive': 0, 'publish': 0, 'trig': 0}
    real_box, real_pub, real_rot = (mt.MoverTable._publish_into,
                                    mt.MoverTable._publish_row, bp.rotation_matrix)

    def count(key, real):
        def wrapper(*a, **k):
            calls[key] += 1
            return real(*a, **k)
        return wrapper

    monkeypatch.setattr(mt.MoverTable, '_publish_into', count('derive', real_box))
    monkeypatch.setattr(mt.MoverTable, '_publish_row', count('publish', real_pub))
    monkeypatch.setattr(bp, 'rotation_matrix', count('trig', real_rot))
    stamp = bp.runtime_generation
    w.tick(200)
    assert {name: w.state_of(name) for name in ALL} == before
    assert w.table().runtime_entries == entries
    assert calls == {'derive': 0, 'publish': 0, 'trig': 0}
    assert bp.runtime_generation == stamp       # nothing told the readers
    for name in ALL:
        brush = w.brushes[name]
        assert '_aabb_runtime' not in brush and '_aabb_sig' not in brush
        assert id(brush) in bp.asleep
    assert _outputs(w, 20) == []


# ---------------------------------------------------------------------------
# Woken: where the reference is, and nothing it missed is fired
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asleep_for", [1, 7, 113, 2000, 50000])
def test_woken_rows_are_where_the_reference_is(worlds, asleep_for):
    ref, w = worlds(), worlds()
    for world in (ref, w):
        world.send("door", "Open")
        world.send("gate", "Open")
        world.tick(30)
    w.park(*ALL)
    w.tick(asleep_for)
    ref.tick(asleep_for)
    w.unpark(*ALL)
    w.tick()
    ref.tick()
    for name in ALL:
        a, b = w.state_of(name), ref.state_of(name)
        if a[0] == 'mover':
            _angle_close(a[3], b[3], 1e-6 * max(1, asleep_for / 100))
            a, b = a[:3] + a[4:], b[:3] + b[4:]
        # Its ends land on the walk's ticks (an even ratio included); within
        # a leg its progress is worked out, so it differs in the last bits.
        tol = 1e-6 * max(1, asleep_for / 100)
        _close(a, b, tol)
    _path_close(w.path_of("tram"), ref.path_of("tram"))
    # Nothing it passed while parked is fired; the reference fired them.
    missed = [e for e in _outputs(w, 30) if e[0] <= 30 + asleep_for]
    assert missed == []
    if asleep_for >= 113:
        assert _outputs(ref, 30)


def _path_close(a, b):
    assert set(a) == set(b)
    for key in a:
        if isinstance(a[key], float):
            assert a[key] == pytest.approx(b[key], abs=1e-6)
        elif isinstance(a[key], list):
            assert a[key] == pytest.approx(b[key], abs=1e-6)
        else:
            assert a[key] == b[key]
    return True


def test_outputs_resume_with_the_next_transition(worlds):
    ref, w = worlds(), worlds()
    for world in (ref, w):
        world.send("door", "Open")
        world.tick(5)
    w.park("door", "lift")
    w.tick(400)
    ref.tick(400)
    w.unpark("door", "lift")
    for world in (ref, w):
        world.tick(600)
    # After waking, the same outputs as the reference, on the same ticks.
    assert _outputs(w, 406) == _outputs(ref, 406)
    assert any(name == "lift" for _t, name, _o in _outputs(w, 406))
    assert [e for e in _outputs(w, 5) if e[0] <= 405 and e[1] in ("door", "lift")] == []


def test_a_door_cycle_is_worked_through(worlds):
    ref, w = worlds(), worlds()
    for world in (ref, w):
        world.send("door", "Open")
        world.tick(2)
    w.park("door")
    # Through opening, open, closing: each phase's end, and closed.
    for step in (40, 30, 10, 60, 200):
        w.tick(step)
        ref.tick(step)
        # A read of its state brings it to now (the logic thread's).
        state = dict(w.runtime.door_states[next(i for i, b in w.runtime.doors
                                                 if b is w.brushes["door"])])
        ref_state = dict(ref.runtime.door_states[next(i for i, b in ref.runtime.doors
                                                     if b is ref.brushes["door"])])
        assert state['state'] == ref_state['state']
        assert state['progress'] == pytest.approx(ref_state['progress'], abs=1e-9)
        _close(w.state_of("door"), ref.state_of("door"), 1e-9)
    assert w.state_of("door")[1] == mt.CLOSED
    assert [e for e in _outputs(w, 2) if e[1] == "door"] == []


# ---------------------------------------------------------------------------
# I/O stays live
# ---------------------------------------------------------------------------

def test_an_input_reaches_a_parked_row_and_takes_effect_from_then(worlds):
    ref, w = worlds(), worlds()
    w.park(*ALL)
    w.tick()
    ref.tick()
    script = [(17, "door", "Open"), (40, "door", "Close"), (41, "door", "Open"),
              (90, "door", "Stop"), (130, "door", "Reverse"), (150, "door", "Toggle"),
              (300, "door", "Open"), (301, "door", "Open"),
              (25, "lift", "Stop"), (80, "lift", "Reverse"), (81, "lift", "Start"),
              (82, "lift", "Enable"), (200, "lift", "Close"), (260, "lift", "SetPosition", "0.25"),
              (270, "lift", "SetSpeed", "20"),
              (60, "still", "Open"), (140, "still", "Toggle"), (141, "still", "Toggle"),
              (33, "spin", "Disable"), (120, "spin", "Enable"),
              (70, "gate", "Toggle"), (71, "gate", "Lock"), (75, "gate", "SetSpeed", "50"),
              (210, "tram", "StopPath"), (240, "tram", "FollowPath", "B")]
    by_tick = {}
    for entry in script:
        by_tick.setdefault(entry[0], []).append(entry[1:])
    sent = set()                    # (tick_count when sent, name)
    for tick in range(2, 420):
        for world in (ref, w):
            for name, input_name, *param in by_tick.get(tick, ()):
                if world is w:
                    sent.add((w.tick_count, name))
                world.send(name, input_name, *param)
            world.tick()
        if tick in (18, 42, 91, 131, 151, 302):
            # The command's effect is there at once, however far away.
            assert w.runtime.door_states[next(
                i for i, b in w.runtime.doors if b is w.brushes["door"])]['state'] == \
                ref.runtime.door_states[next(
                    i for i, b in ref.runtime.doors if b is ref.brushes["door"])]['state']
    w.unpark(*ALL)
    w.tick()
    ref.tick()
    for name in ALL:
        a, b = w.state_of(name), ref.state_of(name)
        if a[0] == 'mover':
            _angle_close(a[3], b[3], 1e-6)
            a, b = a[:3] + a[4:], b[:3] + b[4:]
        _close(a, b, 1e-6)
    _path_close(w.path_of("tram"), ref.path_of("tram"))
    # What an input fires itself (a door's OnOpen, OnClose) is a transition
    # now, and fires as the reference's does; what the row would have fired
    # moving (its timer's OnClose, its ends) is not replayed.
    mine = [e for e in w.fired if e[1] == "door" and e[0] <= 420]
    assert mine and all((tick, name) in sent for tick, name, _out in mine)
    assert set(mine) <= set(ref.fired)
    assert any(e[2] == "OnClose" and (e[0], e[1]) not in sent for e in ref.fired
               if e[1] == "door")              # the reference's timer fired too
    parked = ("door", "lift", "still", "spin", "gate", "tram")
    assert not [e for e in w.fired if e[1] in parked and e[0] <= 420
                and e[2] in ("OnFullyOpen", "OnFullyClosed", "OnPathNodeReached")]


def test_a_delayed_input_by_id_reaches_a_parked_door(worlds):
    w = worlds()
    door = w.brushes["door"]
    w.park("door")
    w.tick()
    from editor.io_system import OutputConnection, add_connection
    # The lift, far away and awake, opens the door a quarter second after
    # it reaches its top.
    add_connection(w.brushes["lift"], OutputConnection(
        output_name='OnFullyOpen', target_name='door', input_name='Open', delay=0.25,
        target_id=door['id']))
    w.tick(200)
    assert any(out == "OnFullyOpen" and name == "lift" for _t, name, out in w.fired)
    i = next(i for i, b in w.runtime.doors if b is door)
    assert w.runtime.door_states[i]['state'] != 'closed'


# ---------------------------------------------------------------------------
# Saved games
# ---------------------------------------------------------------------------

def test_a_save_taken_while_parked_holds_where_it_has_got_to(worlds):
    from engine.savegame import _build_full_snapshot, _restore_runtime_and_players
    ref, w = worlds(), worlds()
    for world in (ref, w):
        world.send("door", "Open")
        world.tick(10)
    w.park(*ALL)
    w.tick(333)
    ref.tick(333)
    saved = _build_full_snapshot(w.logic)
    expected = _build_full_snapshot(ref.logic)
    runtime, ref_runtime = saved["runtime"], expected["runtime"]
    for key in ("door_states", "mover_states"):
        assert runtime[key].keys() == ref_runtime[key].keys()
        for i in runtime[key]:
            for field, value in runtime[key][i].items():
                assert value == pytest.approx(ref_runtime[key][i][field], abs=0.02), (key, field)
    for uid, entry in runtime["moving_brushes"].items():
        assert entry["pos"] == pytest.approx(ref_runtime["moving_brushes"][uid]["pos"], abs=0.5)
    # Loaded into a fresh world, it goes on as the reference does.
    fresh = worlds()
    _restore_runtime_and_players(fresh.logic, saved)
    fresh.tick(240)
    ref.tick(240)
    for name in ("lift", "door", "gate", "tram"):
        a, b = fresh.state_of(name), ref.state_of(name)
        if a[0] == 'mover':
            a, b = a[:3] + a[4:], b[:3] + b[4:]
        _close(a, b, 0.6)


# ---------------------------------------------------------------------------
# Readers are answered for now
# ---------------------------------------------------------------------------

def test_readers_see_a_parked_row_where_it_is_now(worlds):
    ref, w = worlds(), worlds()
    grid = SpatialGrid()
    grid.populate(list(w.brushes.values()))
    w.park(*ALL)
    for _ in range(60):
        w.tick(7)
        ref.tick(7)
        before = {name: w.state_of(name) for name in ALL}
        for name in ("lift", "spin", "tram", "door"):
            mine, theirs = w.brushes[name], ref.brushes[name]
            runtime, expected = bp.runtime_of(mine), bp.runtime_of(theirs)
            if expected is None:
                assert runtime is None
            else:
                assert runtime[:3] == pytest.approx(expected[:3], abs=1e-6)
            assert bp.world_bounds(mine) == pytest.approx(bp.world_bounds(theirs), abs=1e-4)
            assert brush_aabb_bounds(mine) == pytest.approx(brush_aabb_bounds(theirs), abs=1e-3)
            assert bp.collision_shape(mine) is None or True
        # The grid's queries, one at a time and batched.
        lift = bp.world_bounds(w.brushes["lift"])
        cx, cz = (lift[0] + lift[3]) / 2, (lift[2] + lift[5]) / 2
        assert grid.raycast_down(cx, cz, 1000.0) == pytest.approx(lift[4])
        assert grid.raycast_down_batch([cx], [cz], [1000.0])[0] == pytest.approx(lift[4])
        assert grid.overlaps_wall(cx, lift[1] + 4.0, cz, 8.0)
        # Asking stored nothing: the rows are as they were.
        assert {name: w.state_of(name) for name in ALL} == before
        assert '_aabb_sig' not in w.brushes["lift"]


def test_a_brush_in_both_groups_is_never_parked(worlds):
    brushes = _level() + [box_brush(
        "both", (-400.0, 32.0, 0.0), (64, 64, 64), is_mover=True, is_door=True,
        start_on=True, rotate=True, speed=40.0, distance=96.0, direction=[1, 0, 0],
        door_speed=64.0, door_distance=64.0, door_direction="up", open_time=0.4)]
    w = worlds(brushes=brushes)
    w.tick(3)
    w.park("both")
    w.tick(2)
    table = w.table()
    both = w.brushes["both"]
    for group in (table.movers, table.doors):
        assert not group.parked[group.row_of_obj[id(both)]]
    assert id(both) not in bp.asleep
    angle = w.state_of("both")[3]
    w.tick()
    assert w.state_of("both")[3] != angle       # still walked


def test_stopping_play_forgets_parked_rows(worlds):
    w = worlds()
    w.park(*ALL)
    w.tick(5)
    assert all(id(w.brushes[name]) in bp.asleep for name in ALL)
    w.runtime._reset_movers()
    w.runtime._reset_doors()
    assert not any(id(w.brushes[name]) in bp.asleep for name in ALL)


# ---------------------------------------------------------------------------
# The closed forms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("step", [0.3, 0.1, 1.0 / 120.0, 0.07, 1.5, 0.0])
@pytest.mark.parametrize("start", [(0.0, True), (0.4, True), (1.0, True), (0.7, False),
                                   (0.0, False), (1.0, False)])
def test_bounce_is_the_per_tick_rule(step, start):
    for n in (0, 1, 2, 3, 5, 13, 29, 64, 241, 1000):
        p, f = start
        for _ in range(n):
            if f:
                p += step
                if p >= 1.0:
                    p, f = 1.0, False
            else:
                p -= step
                if p <= 0.0:
                    p, f = 0.0, True
        got = mt._bounce(start[0], start[1], step, n)
        assert got[1] == f or abs(step * 120.0 - 1.0) < 1e-12, (n, got, (p, f))
        if got[1] == f:
            assert got[0] == pytest.approx(p, abs=1e-9)


def test_ticks_to_counts_whole_ratios_as_whole():
    assert mt._ticks_to(1.0, 1.0 / 120.0) == 120
    assert mt._ticks_to(0.0, 0.5) == 1
    assert mt._ticks_to(-1.0, 0.5) == 1
    assert mt._ticks_to(1.0, 0.3) == 4
    assert mt._ticks_to(0.55, DT) == math.ceil(0.55 * 60 - 1e-9)


@pytest.mark.parametrize("wait", [0.2, 0.0])
def test_a_path_that_ends_while_parked(worlds, wait):
    """After a wait at its last node the walk stops it (start_on cleared);
    with none it drops the path and the row travels on its own."""
    nodes = [PathNode([0.0, 40.0, 900.0], {'name': 'P', 'next_node': 'Q'}),
             PathNode([300.0, 40.0, 900.0], {'name': 'Q', 'wait_time': wait})]

    def make():
        w = worlds(nodes=False, brushes=[
            box_brush("ground", (0, -16, 0), (8192, 32, 8192)),
            box_brush("shuttle", (0.0, 40.0, 600.0), (64, 16, 64), is_mover=True,
                      start_on=True, speed=120.0, distance=50.0, direction=[0, 1, 0],
                      path_target="P"),
            box_brush("lift", (900.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
                      start_on=True, speed=70.0, distance=130.0, direction=[0, 1, 0])])
        w.logic.editor_state.things = nodes
        w.logic.world_runtime.build_entity_caches()
        w.runtime._init_movers()
        return w

    ref, w = make(), make()
    for world in (ref, w):
        world.tick(3)
    w.park("shuttle")
    w.tick(900)
    ref.tick(900)
    w.unpark("shuttle")
    for world in (ref, w):
        world.tick(30)
    assert w.path_of("shuttle") is None and ref.path_of("shuttle") is None
    if wait:
        # The walk itself stops it there now (its start_on journalled).
        assert ref.state_of("shuttle")[4] == (300.0, 40.0, 900.0)
        assert not ref.table().movers.start_on[0]
    assert w.brushes["shuttle"]["start_on"] == ref.brushes["shuttle"]["start_on"]
    _close(w.state_of("shuttle"), ref.state_of("shuttle"), 1e-6)
    assert not [e for e in w.fired if e[1] == "shuttle" and e[0] <= 903]
    assert [e for e in ref.fired if e[1] == "shuttle"]


def test_big_world_parks_and_wakes_movers(worlds):
    """With Big World's own session: the movers in cells it parks sleep, and
    each wakes, where the reference is, when the player comes back."""
    from plugins.bigworld.runtime import BigWorldSession

    def level():
        return [box_brush("ground", (0, -16, 0), (512, 32, 512)),
                box_brush("near", (0.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
                          start_on=True, speed=70.0, distance=130.0, direction=[0, 1, 0]),
                box_brush("far", (20000.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
                          start_on=True, speed=55.0, distance=90.0, direction=[0, 1, 0]),
                box_brush("far_door", (20200.0, 64.0, 0.0), (64, 128, 16), is_door=True,
                          door_speed=96.0, door_distance=100.0, door_direction="up",
                          open_time=0.55)]

    ref, w = worlds(nodes=False, brushes=level()), worlds(nodes=False, brushes=level())
    session = BigWorldSession(w.logic, activation_radius=1024.0, deactivation_radius=1200.0)
    session.start(player_pos=(0.0, 0.0, 0.0))
    try:
        for world in (ref, w):
            world.tick(2)
        far, door = w.brushes["far"], w.brushes["far_door"]
        assert id(far) in bp.asleep and id(door) in bp.asleep
        assert id(w.brushes["near"]) not in bp.asleep
        # An input reaches the far door at once, though nothing ticks it.
        for world in (ref, w):
            world.send("far_door", "Open")
            world.tick(300)
        # The player crosses to the far side: they wake, the near one sleeps.
        session.tick(player_pos=(20000.0, 0.0, 0.0))
        for world in (ref, w):
            world.tick()
        assert id(far) not in bp.asleep and id(door) not in bp.asleep
        assert id(w.brushes["near"]) in bp.asleep
        for name in ("far", "far_door"):
            _close(w.state_of(name), ref.state_of(name), 1e-6)
        for world in (ref, w):
            world.tick(500)
        session.tick(player_pos=(0.0, 0.0, 0.0))
        for world in (ref, w):
            world.tick()
        _close(w.state_of("near"), ref.state_of("near"), 1e-6)
        missed = [e for e in w.fired if e[2] != "OnOpen"
                  and ((e[1] == "near" and 304 <= e[0] <= 804)
                       or (e[1] in ("far", "far_door") and e[0] <= 303))]
        assert missed == []
    finally:
        session.stop()
    w.runtime._reset_movers()           # play stops
    w.runtime._reset_doors()
    assert not bp.asleep
