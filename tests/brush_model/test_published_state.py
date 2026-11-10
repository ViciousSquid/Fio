"""A mover's runtime state is published once per change, and its derived
world state worked out once from it (``docs/brush-model-stage2.md`` §11.8).

The mover table publishes each changed row's transform into its brush's
runtime-only slot and drops that row's derived key -- nothing else. The
first reader that needs the row's world box, float64 bounds or rotation
works them out once (``constants.mover_box``, ``brush_placement.turn_of``)
and every other reader shares them. These tests hold that:

* the published boxes are the resolver's, to the bit, for boxes, convex
  brushes, spinners about a vertical and a tilted axis, doors and a brush
  that is both;
* a mover that does not move derives nothing, however often it is asked;
* a moved mover is derived once per change, however many readers ask, and
  a spinner's rotation is worked out once;
* a change drops only the changed rows' keys -- no blanket invalidation;
* a source that answers first (the editor preview) takes a brush over;
* what a table published is withdrawn when play stops and when the table
  is collected.
"""

import gc

import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                       # noqa: E402
from engine import brush_placement as bp                           # noqa: E402
from engine import constants                                       # noqa: E402
from engine.constants import brush_aabb_bounds                     # noqa: E402
from engine.logic_thread import LogicThread                        # noqa: E402
from engine.threaded_game_state import ThreadedGameState            # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush           # noqa: E402

pytestmark = pytest.mark.qt

DT = 1.0 / 60.0
SLOTS = ('_aabb_sig', '_aabb_bounds', '_aabb_runtime', '_world_bounds', '_aabb_turn')


def _level(start_on=True):
    brushes = [box_brush("ground", (0, -16, 0), (4096, 32, 4096))]
    for k in range(5):
        brushes.append(box_brush(
            "m%d" % k, (k * 300.0, 32.0, 0.0), (96, 32, 96), is_mover=True,
            start_on=start_on, speed=40.0 + k, distance=64.0, direction=[0, 1, 0]))
    brushes.append(box_brush(
        "spin_up", (0.0, 64.0, 400.0), (160, 16, 16), is_mover=True,
        start_on=start_on, rotate=True, speed=90.0, distance=0, rot_axis=[0.0, 1.0, 0.0]))
    brushes.append(box_brush(
        "spin_tilt", (400.0, 64.0, 400.0), (160, 16, 32), is_mover=True,
        start_on=start_on, rotate=True, speed=70.0, distance=48.0,
        direction=[1, 0, 0], rot_axis=[0.3, 1.0, 0.2]))
    brushes.append(angled_brush(
        "ramp", (800.0, 40.0, 400.0), (128, 64, 128), clip_normal=(0.3, 1.0, 0.0),
        is_mover=True, start_on=start_on, speed=64.0, distance=96.0, direction=[1, 0, 0]))
    brushes.append(angled_brush(
        "ramp_spin", (1200.0, 40.0, 400.0), (128, 64, 128), clip_normal=(0.3, 1.0, 0.0),
        is_mover=True, start_on=start_on, rotate=True, speed=50.0, distance=0,
        rot_axis=[0.0, 1.0, 0.0]))
    for k in range(5):
        brushes.append(box_brush(
            "d%d" % k, (k * 200.0, 64.0, -400.0), (64, 128, 16), is_door=True,
            door_speed=96.0 + k, door_distance=96.0, door_direction="up", open_time=0.3))
    brushes.append(box_brush(
        "both", (-400.0, 32.0, 0.0), (64, 64, 64), is_mover=True, is_door=True,
        start_on=start_on, rotate=True, speed=40.0, distance=96.0, direction=[1, 0, 0],
        door_speed=64.0, door_distance=64.0, door_direction="up", open_time=0.4))
    return brushes


class _World:
    def __init__(self, start_on=True, open_doors=True):
        state = EditorState()
        state.brushes = _level(start_on)
        logic = LogicThread(ThreadedGameState(), state)
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        logic.io_manager = None
        logic.mover_runtime._init_movers()
        logic.mover_runtime._init_doors()
        self.logic = logic
        self.runtime = logic.mover_runtime
        self.open_doors = open_doors
        self.movers = [b for b in state.brushes if b.get('is_mover') or b.get('is_door')]

    def tick(self, n=1):
        runtime = self.runtime
        for _ in range(n):
            if self.open_doors:
                for s in runtime.door_states.values():
                    if s['state'] == 'closed':
                        s['state'] = 'opening'
            runtime._update_movers(DT)
            runtime._update_doors(DT)

    def stop(self):
        self.runtime._reset_movers()
        self.runtime._reset_doors()
        self.logic.stop()


@pytest.fixture(autouse=True)
def one_world(monkeypatch):
    """One world at a time, as the game plays (see test_mover_broad_phase)."""
    gc.collect()
    assert not bp.PREVIEW.runtime_entries
    monkeypatch.setattr(bp, '_sources', tuple(
        ref for ref in bp._sources if ref() is bp.PREVIEW))


@pytest.fixture
def world(request):
    w = _World()
    request.addfinalizer(w.stop)
    return w


def _resolved(brush):
    """The resolver's own answer for *brush*, the published slots set aside."""
    held = {key: brush.pop(key) for key in SLOTS if key in brush}
    try:
        return brush_aabb_bounds(brush), bp.world_bounds(brush)
    finally:
        for key in SLOTS:
            brush.pop(key, None)
        brush.update(held)


def test_published_boxes_are_the_resolvers_to_the_bit(world):
    for ticks in (1, 7, 30, 61):
        world.tick(ticks)
        for brush in world.movers:
            assert '_aabb_runtime' in brush, brush['name']
            published = (brush_aabb_bounds(brush), bp.world_bounds(brush))
            assert published == _resolved(brush), brush['name']
            assert all(type(v) is float for v in published[0] + published[1])


def test_a_still_mover_derives_nothing(monkeypatch, request):
    w = _World(start_on=False, open_doors=False)
    request.addfinalizer(w.stop)
    w.tick(3)
    for brush in w.movers:
        brush_aabb_bounds(brush)
        bp.world_bounds(brush)
    calls = {'derive': 0, 'trig': 0}
    real_box, real_rot = constants.mover_box, bp.rotation_matrix

    def derive(*a, **k):
        calls['derive'] += 1
        return real_box(*a, **k)

    def trig(*a, **k):
        calls['trig'] += 1
        return real_rot(*a, **k)

    monkeypatch.setattr(constants, 'mover_box', derive)
    monkeypatch.setattr(bp, 'rotation_matrix', trig)
    for _ in range(50):
        w.tick()
        for brush in w.movers:
            for _ in range(5):
                brush_aabb_bounds(brush)
                bp.world_bounds(brush)
            bp.collision_shape(brush)
    assert calls == {'derive': 0, 'trig': 0}


def test_a_moved_mover_is_derived_once_per_change(world, monkeypatch):
    world.tick(5)
    calls = {'derive': 0, 'trig': 0}
    real_box, real_rot = constants.mover_box, bp.rotation_matrix

    def derive(*a, **k):
        calls['derive'] += 1
        return real_box(*a, **k)

    def trig(*a, **k):
        calls['trig'] += 1
        return real_rot(*a, **k)

    monkeypatch.setattr(constants, 'mover_box', derive)
    monkeypatch.setattr(bp, 'rotation_matrix', trig)
    spinners = [b for b in world.movers if b.get('rotate')]
    for _ in range(40):
        before = {id(b): b.get('_aabb_runtime') for b in world.movers}
        calls.update(derive=0, trig=0)
        world.tick()
        changed = [b for b in world.movers if b.get('_aabb_runtime') is not before[id(b)]]
        assert changed
        # Many readers, many times: the player's AABB passes, AI wall
        # tests, the collision frame.
        for brush in world.movers:
            for _ in range(10):
                brush_aabb_bounds(brush)
                bp.world_bounds(brush)
            bp.collision_shape(brush)
        assert calls['derive'] == len(changed)
        turned = [b for b in spinners if b in changed]
        assert calls['trig'] == len(turned)


def test_a_change_drops_only_the_rows_that_changed(request):
    # Doors left shut: at rest while the movers move.
    world = _World(open_doors=False)
    request.addfinalizer(world.stop)
    world.tick(3)
    for brush in world.movers:
        brush_aabb_bounds(brush)
    keyed = {id(b): b.get('_aabb_sig') for b in world.movers}
    before = {id(b): b.get('_aabb_runtime') for b in world.movers}
    world.tick()
    for brush in world.movers:
        if brush.get('_aabb_runtime') is before[id(brush)]:
            # Not changed: its derived key survived every stamp of the tick.
            assert brush.get('_aabb_sig') is keyed[id(brush)], brush['name']
        else:
            assert '_aabb_sig' not in brush, brush['name']
    assert any(b.get('_aabb_runtime') is before[id(b)] for b in world.movers)


def test_the_editor_preview_takes_a_brush_over(world):
    world.tick(10)
    lift = next(b for b in world.movers if b['name'] == 'm2')
    bp.PREVIEW.set(lift, centre=(1.0, 500.0, 2.0))
    try:
        world.tick(2)
        assert '_aabb_runtime' not in lift
        lo = bp.world_bounds(lift)
        assert lo[1] == 500.0 - 16.0 and lo[0] == 1.0 - 48.0
        assert brush_aabb_bounds(lift) == _resolved(lift)[0]
    finally:
        bp.PREVIEW.clear()
    world.tick()
    assert '_aabb_runtime' in lift
    assert (brush_aabb_bounds(lift), bp.world_bounds(lift)) == _resolved(lift)


def test_what_a_table_published_is_withdrawn():
    w = _World()
    w.tick(10)
    assert all('_aabb_runtime' in b for b in w.movers)
    w.runtime._reset_movers()
    w.runtime._reset_doors()
    assert not any(key in b for b in w.movers for key in SLOTS)
    w.logic.stop()
    # A world stopped mid-play, never reset: collecting its table withdraws.
    # (The plugin manager keeps the last logic thread until another
    # replaces it, as the next play does.)
    w = _World()
    w.tick(10)
    brushes = list(w.movers)
    assert all('_aabb_runtime' in b for b in brushes)
    w.logic.stop()
    del w
    _World().stop()
    gc.collect()
    assert not any(key in b for b in brushes for key in ('_aabb_runtime', '_aabb_sig'))
