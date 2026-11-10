"""The player's mover broad phase and the grid it reads
(``docs/brush-model-stage2.md`` §11.8).

The spatial grid files every mover and door for its whole travel
(``mover_table.travel_range``), and ``Player.update`` takes the movers and
doors filed in the cells within its reach -- plus any off their filed travel
(``brush_placement.off_range``) -- gathering each brush once. These tests
hold that:

* the player moves exactly as it does when it looks at every mover -- bit
  for bit, tick by tick, through box, angled, spinning and twin movers and
  doors, with jumps, ducks, dashes and pushes that carry it out of what the
  broad phase covered (whose passes then run again on every mover);
* a mover far from the player costs the player's update nothing, and each
  mover the player does look at is gathered once a tick;
* every mover whose box can reach the player is a candidate;
* a mover is found by the grid's queries anywhere along its travel;
* a row taken off its filed travel -- a path, a restored placement, an
  edit in play -- is found everywhere until play stops, and a row that
  stays on its travel is not marked.
"""

import gc
import math

import glm
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                       # noqa: E402
from engine import brush_placement as bp                           # noqa: E402
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
    elsewhere leave sources registered, some still holding entries, so each
    test here starts from the preview alone (restored afterwards)."""
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
    counts = {'asked': 0, 'rows': 0, 'near': 0, 'gathers': 0, 'updates': 0}
    real_near, real_gather, real_update = (
        Player._near_movers, player_module._gather, Player.update)

    def near(self, grid, colliders, moving):
        counts['asked'] += 1
        cover, kept = real_near(self, grid, colliders, moving)
        counts['rows'] += len(moving)
        counts['near'] += len(kept)
        return cover, kept

    def gather(*args):
        counts['gathers'] += 1
        return real_gather(*args)

    def update(self, *args, **kwargs):
        counts['updates'] += 1
        return real_update(self, *args, **kwargs)

    monkeypatch.setattr(Player, '_near_movers', near)
    monkeypatch.setattr(player_module, '_gather', gather)
    monkeypatch.setattr(Player, 'update', update)
    return counts


def test_the_player_moves_as_it_does_over_every_mover(counted, monkeypatch):
    """Bit for bit, every tick, with the grid's broad phase and looking at
    every mover; the broad phase did cut the lists, and the passes did fall
    back to every mover when the player went past what it covered."""
    with_grid = _trajectory()
    assert counted['asked'] == counted['updates'] == 900
    assert counted['near'] < counted['rows'] / 2        # most movers left out
    assert counted['gathers'] - counted['updates'] > 20  # ... and fallbacks ran
    monkeypatch.setattr(Player, '_near_movers',
                        lambda self, grid, colliders, moving: (None, moving))
    everything = _trajectory()
    assert len({row[8] for row in everything}) > 3      # it stood on several shapes
    assert sum(row[7] for row in everything) > 50        # and ducked
    assert with_grid == everything


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


def test_each_mover_is_gathered_once_a_tick(monkeypatch):
    """The grid's cells hand the player the movers filed near it, and so do
    its own candidates: each is gathered -- and narrow-phased -- once."""
    run = _Run()
    try:
        seen = []
        real = player_module._blocks_player

        def blocks(brush):
            seen.append(id(brush))
            return real(brush)

        monkeypatch.setattr(player_module, '_blocks_player', blocks)
        twice = 0
        movers = {id(b) for b in run.runtime._mover_brush_list + run.runtime._door_brush_list}
        for tick in range(300):
            del seen[:]
            run.step(tick, events=False)
            ids = [key for key in seen if key in movers]
            twice += len(ids) - len(set(ids))
        assert twice == 0
    finally:
        run.close()


def test_every_mover_that_can_reach_the_player_is_a_candidate():
    run = _Run()
    try:
        player = run.player
        moving = player._moving(run.runtime._mover_brush_list, run.runtime._door_brush_list)
        for tick in range(240):
            run.step(tick)
            pads = player._reach_pads
            pad, pad_turned = pads[1], pads[2]
            _cover, near = player._near_movers(run.grid, [], moving)
            kept = {id(b) for b in near}
            x, y, z = player.pos.x, player.pos.y, player.pos.z
            for brush in moving:
                lo = bp.world_bounds(brush)
                p = pad_turned if brush.get('rotate') else pad
                if all(lo[i] - p[i] <= (x, y, z)[i] <= lo[i + 3] + p[i] for i in range(3)):
                    assert id(brush) in kept, brush['name']
            # In canonical order, each once.
            order = [moving.index(b) for b in near]
            assert order == sorted(set(order))
    finally:
        run.close()


def test_a_mover_is_found_anywhere_along_its_travel():
    """The grid used to file a mover where play began: one that travelled
    into another cell was missed there by every grid query."""
    state = EditorState()
    slider = box_brush("slider", (400.0, 64.0, 100.0), (64, 64, 64), is_mover=True,
                       start_on=True, speed=400.0, distance=900.0, direction=[1, 0, 0])
    state.brushes = [box_brush("ground", (0, -16, 0), (8192, 32, 8192)), slider]
    logic = LogicThread(ThreadedGameState(), state)
    logic.session_runtime.play_mode = True
    logic.world_runtime.build_entity_caches()
    logic.io_manager = None
    logic.mover_runtime._init_movers()
    logic.mover_runtime._init_doors()
    grid = SpatialGrid()
    grid.populate(state.brushes)
    try:
        cells = set()
        for _ in range(140):
            logic.mover_runtime._update_movers(DT)
            cx, cy, cz = bp.world_box(slider)[:3]
            cells.add(int(math.floor(cx / 512.0)))
            assert grid.overlaps_wall(cx, cy - 20.0, cz, 8.0)
            assert grid.raycast_down(cx, cz, 500.0) == pytest.approx(cy + 32.0)
            ys = grid.raycast_down_batch([cx], [cz], [500.0])
            assert ys[0] == pytest.approx(cy + 32.0)
        assert len(cells) >= 3                  # it crossed into other cells
        assert not bp.off_range
    finally:
        logic.mover_runtime._reset_movers()
        logic.mover_runtime._reset_doors()
        logic.stop()


def test_a_row_off_its_travel_is_found_everywhere():
    run = _Run()
    try:
        run.step(0)
        table = run.runtime._movers()
        lift = next(b for b in run.runtime._mover_brush_list if b['name'] == 'm0')
        assert not bp.off_range
        # A path step far from where the grid filed it.
        table.move_to(lift, (3000.0, 64.0, 3000.0))
        assert id(lift) in bp.off_range
        assert run.grid.overlaps_wall(3000.0, 40.0, 3000.0, 8.0)
        assert run.grid.raycast_down(3000.0, 3000.0, 500.0) == pytest.approx(64.0 + 16.0)
        assert run.grid.raycast_down_batch([3000.0], [3000.0], [500.0])[0] == pytest.approx(80.0)
        assert not run.grid.has_line_of_sight(glm.vec3(2900.0, 64.0, 3000.0),
                                              glm.vec3(3100.0, 64.0, 3000.0))
        player = run.player
        moving = player._moving(run.runtime._mover_brush_list, run.runtime._door_brush_list)
        player.pos = glm.vec3(3000.0, 130.0, 3000.0)
        _cover, near = player._near_movers(run.grid, [], moving)
        assert lift in near
    finally:
        run.close()
    assert not bp.off_range                     # play stopped


def test_restores_and_edits_mark_a_row_only_when_it_leaves_its_travel():
    run = _Run()
    try:
        run.step(0)
        table = run.runtime._movers()
        movers = run.runtime._mover_brush_list
        # Door states loaded at play start (every door) mark nothing.
        assert not bp.off_range
        # A saved placement on its travel: not marked; off it: marked.
        lift = next(b for b in movers if b['name'] == 'm1')
        table.restore_placement(lift, (lift['pos'][0], lift['pos'][1] + 20.0, lift['pos'][2]))
        assert id(lift) not in bp.off_range
        table.restore_placement(lift, (lift['pos'][0] + 600.0, lift['pos'][1], lift['pos'][2]))
        assert id(lift) in bp.off_range
        # A saved spin on a mover that does not turn.
        other = next(b for b in movers if b['name'] == 'm2')
        table.restore_placement(other, other['pos'], angle=30.0)
        assert id(other) in bp.off_range
        # A journaled edit that lengthens a mover's travel past its filing.
        far = next(b for b in movers if b['name'] == 'm3')
        run.logic.editor_state.save_state()
        far['distance'] = 2000.0
        touch(far)
        run.step(1)
        assert id(far) in bp.off_range
        near = next(b for b in movers if b['name'] == 'm4')
        assert id(near) not in bp.off_range
    finally:
        run.close()


@pytest.mark.parametrize("bad", [float('nan'), float('inf'), -float('inf')])
def test_a_position_no_cell_holds_looks_at_every_mover(bad):
    run = _Run()
    try:
        run.step(0)
        player = run.player
        moving = player._moving(run.runtime._mover_brush_list, run.runtime._door_brush_list)
        player.pos.x = bad
        cover, near = player._near_movers(run.grid, [], moving)
        assert cover is None and near is moving
    finally:
        run.close()
