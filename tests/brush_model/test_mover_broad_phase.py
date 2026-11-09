"""The player's mover broad phase (``docs/brush-model-stage2.md`` §11.7).

``Player.update`` hands its passes only the movers and doors the mover
table's columns say are near (``brush_placement.movers_near`` ->
``MoverTable.near_rows``). These tests hold it to the passes it replaces:

* the player moves exactly as it does when it looks at every mover --
  bit for bit, tick by tick, through box, angled, spinning and twin movers
  and doors, with jumps, ducks, dashes and pushes that carry it out of what
  the broad phase covered (whose passes then run again on every mover);
* a mover far from the player costs the player's update nothing;
* the answer is a superset of every row whose box can reach that far, by
  the scan and by the vectorised pass alike;
* it steps aside whenever the table is not the one answering (the editor
  preview, a test's overrides, lists that are not the table's rows);
* a row's reach follows a journaled edit and a restored spin.
"""

import gc
import math

import glm
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                       # noqa: E402
from engine import brush_placement as bp                           # noqa: E402
from engine import mover_table as mt                               # noqa: E402
from engine import player as player_module                         # noqa: E402
from engine.change_journal import touch                            # noqa: E402
from engine.logic_thread import LogicThread                        # noqa: E402
from engine.physics import SpatialGrid                             # noqa: E402
from engine.player import Player                                   # noqa: E402
from engine.threaded_game_state import ThreadedGameState            # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush           # noqa: E402

pytestmark = pytest.mark.qt

DT = 1.0 / 60.0


@pytest.fixture(autouse=True)
def one_world(monkeypatch):
    """The game plays one world at a time: the editor preview (empty in play)
    and the playing world's mover table are the only runtime sources. Tests
    elsewhere leave sources registered, some still holding entries -- for
    which the broad phase would rightly step aside -- so each test here
    starts from the preview alone (the list is restored afterwards)."""
    gc.collect()
    assert not bp.PREVIEW.runtime_entries
    monkeypatch.setattr(bp, '_sources', tuple(
        ref for ref in bp._sources if ref() is bp.PREVIEW))


def _level():
    """A row of box, angled, spinning and turned movers and doors beside
    the player's path, a brush that is both, and doors far away."""
    brushes = [box_brush("ground", (0, -16, 0), (8192, 32, 8192))]
    for k in range(6):
        brushes.append(box_brush(
            "m%d" % k, (-600.0 + k * 220.0, 24.0, 0.0), (96, 32, 96), is_mover=True,
            start_on=True, speed=30.0 + 4 * k, distance=48.0, direction=[0, 1, 0]))
    for k in range(4):
        brushes.append(box_brush(
            "s%d" % k, (-500.0 + k * 300.0, 40.0, 90.0), (160, 16, 16), is_mover=True,
            start_on=True, rotate=True, speed=60.0 + 10 * k, distance=32.0,
            direction=[0, 1, 0], rot_axis=[0.0, 1.0, 0.0] if k % 2 else [0.3, 1.0, 0.2]))
    for k in range(3):
        brushes.append(angled_brush(
            "a%d" % k, (-400.0 + k * 350.0, 40.0, -90.0), (96, 64, 96),
            clip_normal=(0.35, 1.0, 0.0), is_mover=True, start_on=True,
            rotate=k == 1, speed=40.0, distance=40.0, direction=[0, 1, 0]))
    for k in range(12):
        brushes.append(box_brush(
            "d%d" % k, (-900.0 + k * 160.0, 64.0, 140.0 if k < 6 else 3000.0),
            (64, 128, 16), is_door=True, door_speed=96.0 + k, door_distance=96.0,
            door_direction="up", open_time=0.3))
    brushes.append(box_brush(
        "both", (700.0, 32.0, 0.0), (64, 64, 64), is_mover=True, is_door=True,
        start_on=True, rotate=True, speed=40.0, distance=96.0, direction=[1, 0, 0],
        door_speed=64.0, door_distance=64.0, door_direction="up", open_time=0.4))
    return brushes


class _Run:
    """A level in play and a player walking through it, the logic thread's
    order: the mover table ticks, then the player is updated with every
    mover and door in the level."""

    def __init__(self):
        state = EditorState()
        state.brushes = _level()
        logic = LogicThread(ThreadedGameState(), state)
        logic.session_runtime.play_mode = True
        logic.world_runtime.build_entity_caches()
        self.player = Player(-800.0, 0.0, angle=0.0)
        self.player.pos.y = 30.0
        logic.player_runtime.player = self.player
        logic.io_manager = None
        logic.mover_runtime._init_movers()
        logic.mover_runtime._init_doors()
        self.grid = SpatialGrid()
        self.grid.populate(state.brushes)
        self.statics = [b for b in state.brushes
                        if not (b.get('is_mover') or b.get('is_door'))]
        self.logic = logic
        self.runtime = logic.mover_runtime

    def step(self, tick, move=glm.vec3(1.0, 0.0, 0.0), events=True):
        runtime = self.runtime
        for s in runtime.door_states.values():
            if s['state'] == 'closed':
                s['state'] = 'opening'
        runtime._update_movers(DT)
        runtime._update_doors(DT)
        player = self.player
        if events and tick % 211 == 90:
            player.pos.y = 700.0                       # a long fall
        if events and tick % 173 == 40:
            player.velocity.x = 6000.0                 # far past the broad phase
        if events and tick % 97 == 60:
            # Inside the nearest mover, where it is now: a long push out.
            shapes = runtime._mover_brush_list + runtime._door_brush_list
            centres = [bp.world_box(b)[:3] for b in shapes]
            player.pos.x, player.pos.y, player.pos.z = min(
                centres, key=lambda c: abs(c[0] - player.pos.x) + abs(c[2] - player.pos.z))
        player.update(DT, move, tick % 41 == 0, (tick // 60) % 4 == 2, self.statics,
                      runtime._mover_brush_list, runtime._door_brush_list,
                      spatial_grid=self.grid)
        if player.pos.x > 900.0:
            player.pos.x = -800.0
            player.pos.z = (-60.0, 0.0, 60.0, 120.0)[tick % 4]
        g = player.ground_object
        return (player.pos.x, player.pos.y, player.pos.z, player.velocity.x,
                player.velocity.y, player.velocity.z, player.on_ground, player.ducked,
                g.get('name') if isinstance(g, dict) else None)

    def close(self):
        # Stop play as the game does, so no later world sees this table's
        # entries.
        self.runtime._reset_movers()
        self.runtime._reset_doors()
        self.logic.stop()


def _trajectory(ticks=900):
    run = _Run()
    try:
        return [run.step(tick) for tick in range(ticks)]
    finally:
        run.close()


@pytest.fixture
def counted(monkeypatch):
    """Counts the broad phase's answers and the gathers that fell back."""
    counts = {'asked': 0, 'answered': 0, 'rows': 0, 'near': 0, 'gathers': 0, 'updates': 0}
    real_near, real_gather, real_update = (
        player_module.movers_near, player_module._gather, Player.update)

    def near(lists):
        counts['asked'] += 1
        rows = real_near(lists)
        if rows is None:
            return None
        counts['answered'] += 1

        def answer(lists, *args):
            out = rows(lists, *args)
            counts['rows'] += sum(len(x or ()) for x in lists)
            counts['near'] += sum(len(x or ()) for x in out)
            return out
        return answer

    def gather(*args):
        counts['gathers'] += 1
        return real_gather(*args)

    def update(self, *args, **kwargs):
        counts['updates'] += 1
        return real_update(self, *args, **kwargs)

    monkeypatch.setattr(player_module, 'movers_near', near)
    monkeypatch.setattr(player_module, '_gather', gather)
    monkeypatch.setattr(Player, 'update', update)
    return counts


def test_the_player_moves_as_it_does_over_every_mover(counted, monkeypatch):
    """Bit for bit, every tick, with the broad phase scanning, with it
    vectorised, and looking at every mover; the broad phase did cut the
    lists, and the passes did fall back to every mover when the player went
    past what it covered."""
    with_scan = _trajectory()
    assert counted['answered'] == counted['updates'] == 900
    assert counted['near'] < counted['rows'] / 3        # most movers left out
    assert counted['gathers'] - counted['updates'] > 20  # ... and fallbacks ran
    monkeypatch.setattr(mt.MoverTable, 'NEAR_VECTOR_FROM', 0)
    vectorised = _trajectory()
    monkeypatch.setattr(Player, 'MOVER_NEAR_FROM', 10 ** 9)
    everything = _trajectory()
    assert len({row[8] for row in everything}) > 3      # it stood on several shapes
    assert sum(row[7] for row in everything) > 50        # and ducked
    assert with_scan == everything
    assert vectorised == everything


def test_a_far_mover_costs_the_player_nothing(monkeypatch):
    run = _Run()
    try:
        # Walking and jumping (no dash or push past what the broad phase
        # covers, after which a pass looks at every mover again).
        for tick in range(30):
            run.step(tick, events=False)
        far = {id(b) for b in run.runtime._door_brush_list if b['pos'][2] > 1000.0}
        assert len(far) == 6
        touched = []
        real = player_module._blocks_player

        def blocks(brush):
            if id(brush) in far:
                touched.append(brush['name'])
            return real(brush)

        monkeypatch.setattr(player_module, '_blocks_player', blocks)
        for tick in range(30, 300):
            run.step(tick, events=False)
        assert touched == []
    finally:
        run.close()


def _world_near(group, point, pad, pad_turned):
    """Rows whose box, as the resolver places and turns it, comes within
    *pad* of *point* (or *pad_turned*, for a row that can turn): what the
    broad phase must keep."""
    out = []
    for row, brush in enumerate(group.brushes):
        lo = bp.world_bounds(brush)
        turned = bool(group.rotate[row]) if hasattr(group, 'rotate') else False
        p = pad_turned if turned else pad
        if all(lo[i] - p[i] <= point[i] <= lo[i + 3] + p[i] for i in range(3)):
            out.append(brush)
    return out


def test_near_rows_keeps_every_row_that_can_reach(monkeypatch):
    run = _Run()
    try:
        table = run.runtime._movers()
        lists = (run.runtime._mover_brush_list, run.runtime._door_brush_list)
        pad, pad_turned = (40.0, 60.0, 40.0), (150.0, 150.0, 150.0)
        for tick in range(240):
            run.step(tick)
            if tick % 6:
                continue
            assert bp.movers_near(lists) == table.near_rows
            point = (-900.0 + (tick * 37) % 1800, (tick * 13) % 200 - 50.0,
                     (tick * 29) % 400 - 200.0)
            scan = table.near_rows(lists, *point, pad, pad_turned)
            with monkeypatch.context() as patched:
                patched.setattr(mt.MoverTable, 'NEAR_VECTOR_FROM', 0)
                vector = table.near_rows(lists, *point, pad, pad_turned)
            assert [[b['name'] for b in x] for x in scan] == \
                [[b['name'] for b in x] for x in vector]
            for kept, brushes, group in zip(scan, lists, (table.movers, table.doors)):
                names = {b['name'] for b in kept}
                must = _world_near(group, point, pad, pad_turned)
                assert {b['name'] for b in must} <= names
                # In list order.
                assert [b['name'] for b in kept] == [b['name'] for b in brushes
                                                     if b['name'] in names]
                # The twin is always kept.
                assert all(b['name'] in names for b in brushes if b['name'] == 'both')
    finally:
        run.close()


def test_the_broad_phase_steps_aside_for_other_sources():
    run = _Run()
    try:
        run.step(0)
        lists = (run.runtime._mover_brush_list, run.runtime._door_brush_list)
        table = run.runtime._movers()
        assert bp.movers_near(lists) == table.near_rows
        # Lists that are not the table's rows: every mover is looked at.
        assert bp.movers_near((list(reversed(lists[0])), lists[1])) is None
        assert bp.movers_near((lists[0][:-1], lists[1])) is None
        # Another source answering for a brush: likewise.
        bp.PREVIEW.set(lists[0][0], offset=(0.0, 10.0, 0.0))
        try:
            assert bp.movers_near(lists) is None
        finally:
            bp.PREVIEW.clear()
        assert bp.movers_near(lists) == table.near_rows
    finally:
        run.close()


def test_reaches_follow_a_journaled_edit_and_a_restored_spin():
    run = _Run()
    try:
        run.step(0)
        table = run.runtime._movers()
        movers = table.movers
        lists = (run.runtime._mover_brush_list, run.runtime._door_brush_list)
        pad = (10.0, 10.0, 10.0)
        pad_turned = (10.0, 10.0, 10.0)
        lift = next(b for b in lists[0] if b['name'] == 'm0')
        row = movers.row_of_obj[id(lift)]
        before = table._reach(movers, pad, pad_turned)[1][row]
        assert before == (48.0 + 10.0, 16.0 + 10.0, 48.0 + 10.0)
        # A journaled size edit (as the console's setprop makes one),
        # followed by the table at its next tick.
        run.logic.editor_state.save_state()
        lift['size'] = [400.0, 32.0, 96.0]
        touch(lift)
        run.step(1)
        assert table._reach(movers, pad, pad_turned)[1][row][0] == 200.0 + 10.0
        # A saved game's spin on a mover that does not rotate: bounded by
        # the sphere through its corners from now on.
        table.restore_placement(lift, movers.pos[row].tolist(), angle=30.0)
        radius = math.sqrt(200.0 ** 2 + 16.0 ** 2 + 48.0 ** 2)
        got = table._reach(movers, pad, pad_turned)[1][row]
        assert got == pytest.approx((radius + 10.0,) * 3)
    finally:
        run.close()
