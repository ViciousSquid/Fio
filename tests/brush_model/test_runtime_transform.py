"""Stage 2b: the mover table owns every runtime transform.

A mover's or door's travel and spin live in ``engine.mover_table.MoverTable``
(a registered runtime source of ``engine.brush_placement``) and are never
written into the brush: ``pos`` is the authored placement, and
``original_pos``, ``_rot_angle`` and ``rotation_yaw`` are not written at all
(``docs/brush-model-stage2.md``). These tests hold that, and hold every
reader to the table's answer:

* play writes nothing into any brush -- linear movers, spinners, doors,
  path movers, angled movers;
* rendering (published and re-resolved), the AABB cache, collision rows,
  the physics grid, saved games and parented lights all see a mover where
  the table put it, to the bit;
* stopping play returns every mover home with nothing to restore;
* saved games round-trip mid-motion (and older saves restore the same way),
  including a restore before play has built its rows;
* a saved map carries no play state, and an older map that did is loaded
  as authored;
* Big World parks and reactivates a mover mid-motion without moving its
  home or keeping a second copy of where it is;
* the editor's mover preview drives a runtime transform, never ``pos``;
* the plugin accessor (API 1.8.0) and the deathmatch plugin read it.
"""

import copy

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from editor.editor_state import EditorState                       # noqa: E402
from editor.things import Light                                    # noqa: E402
from engine import brush_geometry as bg                            # noqa: E402
from engine import brush_placement as bp                           # noqa: E402
from engine import render_table as rt                              # noqa: E402
from engine import savegame                                         # noqa: E402
from engine.constants import brush_aabb_bounds                     # noqa: E402
from engine.logic_thread import LogicThread                        # noqa: E402
from engine.physics import SpatialGrid                             # noqa: E402
from engine.render_table import RenderTable                        # noqa: E402
from engine.threaded_game_state import ThreadedGameState            # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt

PLAY_KEYS = set(bp.PLAY_STATE_KEYS)


def _world():
    return [
        box_brush("ground", (0, -16, 0), (4096, 32, 4096)),
        box_brush("lift", (300, 16, 0), (128, 32, 128), is_mover=True,
                  start_on=True, speed=96.0, distance=256.0, direction=[0, 1, 0]),
        box_brush("fan", (-300, 64, 0), (256, 16, 32), is_mover=True,
                  start_on=True, rotate=True, speed=90.0, distance=0,
                  rot_axis=[0.0, 1.0, 0.0]),
        box_brush("gate", (0, 64, 300), (128, 128, 16), is_door=True,
                  door_speed=128.0, door_distance=128.0, door_direction="up",
                  open_time=30.0),
        angled_brush("ramp_lift", (0, 40, -400), (128, 64, 128),
                     clip_normal=(0.3, 1.0, 0.0), is_mover=True, start_on=True,
                     speed=64.0, distance=192.0, direction=[1, 0, 0]),
    ]


def _playing(brushes, things=()):
    state = EditorState()
    state.brushes = list(brushes)
    state.things = list(things)
    logic = LogicThread(ThreadedGameState(), state)
    logic.session_runtime.play_mode = True
    logic.world_runtime.build_entity_caches()
    logic.mover_runtime._init_movers()
    logic.mover_runtime._init_doors()
    return logic


def _tick(logic, ticks, dt=1.0 / 60.0):
    for _ in range(ticks):
        logic.mover_runtime._update_movers(dt)
        logic.mover_runtime._update_doors(dt)


def _public(brush):
    return {k: copy.deepcopy(v) for k, v in brush.items() if not k.startswith('_')}


def _named(logic, name):
    return next(b for b in logic.editor_state.brushes if b.get('name') == name)


@pytest.fixture
def logic(request):
    world = _world()
    logic = _playing(world)
    request.addfinalizer(logic.stop)
    gate = _named(logic, "gate")
    logic.mover_runtime._trigger_door_open(logic.editor_state.brushes.index(gate), gate)
    return logic


# ---------------------------------------------------------------------------
# Play writes nothing
# ---------------------------------------------------------------------------

def test_play_never_writes_a_brush(logic):
    before = [_public(b) for b in logic.editor_state.brushes]
    homes = [bp.authored_box(b)[:3] for b in logic.editor_state.brushes]
    _tick(logic, 90)
    for brush, public in zip(logic.editor_state.brushes, before):
        assert _public(brush) == public, brush['name']
        assert not PLAY_KEYS & set(brush), brush['name']
    moved = {b['name']: bp.placed_box(b)[:3] != home
             for b, home in zip(logic.editor_state.brushes, homes)}
    assert moved == {'ground': False, 'lift': True, 'fan': False, 'gate': True,
                     'ramp_lift': True}
    assert bp.runtime_spin(_named(logic, "fan"))[3] > 0.0


def test_the_resolver_hands_over_the_tables_columns_exactly(logic):
    _tick(logic, 77)
    table = logic.mover_runtime._movers()
    for group in (table.movers, table.doors):
        for row, brush in enumerate(group.brushes):
            assert bp.placed_box(brush)[:3] == tuple(group.pos[row].tolist()), brush['name']
    fan = _named(logic, "fan")
    row = table.movers.row_of_obj[id(fan)]
    assert bp.runtime_spin(fan)[3] == float(table.movers.rot_angle[row])


# ---------------------------------------------------------------------------
# Every reader agrees with the table
# ---------------------------------------------------------------------------

def test_rendering_draws_a_mover_where_the_table_put_it(logic):
    _tick(logic, 61)
    brushes = logic.editor_state.brushes
    published = RenderTable()
    published.sync(brushes, 1)
    logic.mover_runtime._movers().publish(logic, published)
    resolved = RenderTable()
    resolved.sync(brushes, 1)                       # warm resolve, no publish
    for slot, brush in enumerate(brushes):
        want = np.asarray(bp.placed_box(brush)[:3])
        assert np.array_equal(published.bounds[slot, :3], want), brush['name']
        assert np.array_equal(resolved.bounds[slot, :3], want), brush['name']
        spin = bp.runtime_spin(brush)
        for table in (published, resolved):
            if spin is None:
                assert float(table.rot[slot, 3]) == 0.0, brush['name']
            else:
                assert tuple(table.rot[slot]) == pytest.approx(spin), brush['name']


def test_an_angled_mover_is_drawn_carried_by_the_table(logic):
    _tick(logic, 45)
    ramp = _named(logic, "ramp_lift")
    slot = logic.editor_state.brushes.index(ramp)
    table = RenderTable()
    table.sync(logic.editor_state.brushes, 1)
    record = table.geometry_records[int(table.geometry_id[slot])]
    models, _ = rt.model_matrices(table, np.array([slot]))
    model = models[0].reshape(4, 4).T.astype(np.float64)
    local = ((record.convex.verts - record.origin) / record.scale).astype(np.float32)
    drawn = (model[:3, :3] @ local.astype(np.float64).T).T + model[:3, 3]
    shift = np.subtract(bp.placed_box(ramp)[:3], bp.authored_box(ramp)[:3])
    assert shift[0] > 1.0
    assert np.allclose(drawn, bg.get_convex(ramp).verts + shift, atol=1e-3)


def test_collision_and_ai_read_a_mover_where_the_table_put_it(logic):
    from engine.logic_collision import CollisionRows
    brushes = logic.editor_state.brushes
    rows = CollisionRows(brushes, 1)
    grid = SpatialGrid()
    grid.populate(brushes)
    _tick(logic, 70)
    rows.refresh_movers()
    grid._cell_rows.refresh_movers()
    for slot, brush in enumerate(brushes):
        want = np.asarray(bp.world_bounds(brush))
        assert np.array_equal(rows.lo[:, slot], want[:3]), brush['name']
        assert np.array_equal(rows.hi[:, slot], want[3:]), brush['name']
        row = grid._cell_rows.brushes.index(brush)
        assert np.array_equal(grid._cell_rows.lo[row], want[:3]), brush['name']
        assert np.allclose(brush_aabb_bounds(brush), want, rtol=1e-6, atol=1e-4), brush['name']


def test_a_parented_light_follows_the_table(request):
    lift = box_brush("lift", (300, 16, 0), (128, 32, 128), is_mover=True,
                     start_on=True, speed=96.0, distance=256.0, direction=[0, 1, 0])
    lamp = make_thing(Light, "lamp", (300, 80, 0), parent_mover="lift")
    logic = _playing([lift], [lamp])
    request.addfinalizer(logic.stop)
    logic.parenting_runtime._init_parented_lights()
    _tick(logic, 50)
    logic.parenting_runtime._update_parented_lights()
    centre = bp.placed_box(lift)[:3]
    assert list(lamp.pos) == pytest.approx([centre[0], centre[1] + 64.0, centre[2]])
    assert centre[1] > 16.0 + 1.0


# ---------------------------------------------------------------------------
# Stopping play
# ---------------------------------------------------------------------------

def test_stopping_play_returns_every_mover_home(logic):
    homes = [bp.authored_box(b)[:3] for b in logic.editor_state.brushes]
    _tick(logic, 80)
    logic.mover_runtime._reset_movers()
    logic.mover_runtime._reset_doors()
    for brush, home in zip(logic.editor_state.brushes, homes):
        assert bp.placed_box(brush)[:3] == home, brush['name']
        assert bp.runtime_spin(brush) is None, brush['name']
    assert not logic.mover_runtime._movers().runtime_entries


# ---------------------------------------------------------------------------
# Saved games
# ---------------------------------------------------------------------------

def _placements(logic):
    return {b['name']: (bp.placed_box(b)[:3], bp.runtime_spin(b))
            for b in logic.editor_state.brushes}


def test_a_save_mid_motion_restores_where_everything_was(logic, request):
    _tick(logic, 53)
    saved = savegame._capture_moving_brushes(logic)
    expected = _placements(logic)
    # A fresh session of the same authored world: nothing has moved, and its
    # rows do not exist yet (the restore waits for them).
    fresh = _playing([copy.deepcopy({k: v for k, v in b.items() if not k.startswith('_')})
                      for b in logic.editor_state.brushes])
    request.addfinalizer(fresh.stop)
    savegame._restore_moving_brushes(fresh, saved)
    fresh.mover_runtime._movers()._link(fresh)
    got = _placements(fresh)
    for name, (centre, spin) in expected.items():
        assert got[name][0] == pytest.approx(centre), name
        if spin is None:
            assert got[name][1] is None, name
        else:
            assert got[name][1][3] == pytest.approx(spin[3]), name
    for brush in fresh.editor_state.brushes:
        assert not PLAY_KEYS & set(brush), brush['name']


def test_an_older_save_restores_through_the_same_mapping(request):
    """A save's ``pos`` was always the brush's centre in play and ``rot`` its
    spin: what the table takes now."""
    world = _world()
    logic = _playing(world)
    request.addfinalizer(logic.stop)
    logic.mover_runtime._movers()._link(logic)
    lift, fan = _named(logic, "lift"), _named(logic, "fan")
    older = {lift['id']: {"pos": [300.0, 140.5, 0.0], "rot": 0.0, "start_on": False},
             fan['id']: {"pos": [-300.0, 64.0, 0.0], "rot": 123.0}}
    savegame._restore_moving_brushes(logic, older)
    assert bp.placed_box(lift)[:3] == (300.0, 140.5, 0.0)
    assert lift['pos'] == [300.0, 16.0, 0.0] and lift['start_on'] is False
    assert bp.runtime_spin(fan)[3] == 123.0


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------

def test_a_map_saved_in_play_carries_no_play_state(logic):
    _tick(logic, 40)
    level = logic.editor_state.get_level_data()
    for record in level['brushes']:
        assert not PLAY_KEYS & set(record), record.get('name')
    lift = next(r for r in level['brushes'] if r.get('name') == 'lift')
    assert lift['pos'] == [300.0, 16.0, 0.0]


def test_an_older_map_with_play_state_loads_as_authored():
    """Seven shipped maps carry ``original_pos`` (one ``rotation_yaw``), all
    saved at rest. A box saved mid-motion is put back where it was authored;
    an angled brush's planes are where it is."""
    resting = box_brush("rest", (10, 0, 0), (64, 64, 64), is_mover=True,
                        original_pos=[10.0, 0.0, 0.0], rotation_yaw=166.4)
    midway = box_brush("mid", (500, 200, 0), (64, 64, 64), is_door=True,
                       original_pos=[500.0, 0.0, 0.0], _rot_angle=30.0)
    ramp = angled_brush("ramp", (0, 40, 0), (128, 64, 128), is_mover=True)
    ramp['original_pos'] = [ramp['pos'][0], ramp['pos'][1] + 50.0, ramp['pos'][2]]
    ramp['pos'] = list(ramp['original_pos'])
    state = EditorState()
    state.load_from_data({'version': 4, 'brushes': [resting, midway, ramp],
                          'things': []}, save_undo=False)
    loaded = {b['name']: b for b in state.brushes}
    for brush in loaded.values():
        assert not PLAY_KEYS & set(brush), brush['name']
    assert loaded['rest']['pos'] == [10.0, 0.0, 0.0]
    assert loaded['mid']['pos'] == [500.0, 0.0, 0.0]
    assert loaded['ramp']['pos'] == pytest.approx(list(bg.get_convex(loaded['ramp']).shape_box[:3]))


# ---------------------------------------------------------------------------
# Big World: parked and reactivated mid-motion
# ---------------------------------------------------------------------------

def test_big_world_parks_and_wakes_a_mover_mid_motion(request):
    from plugins.bigworld.manager import brush_cells
    from plugins.bigworld.runtime import BigWorldSession

    world = _world()
    logic = _playing(world)
    request.addfinalizer(logic.stop)
    session = BigWorldSession(logic, activation_radius=1024.0,
                              deactivation_radius=1200.0)
    session.start(player_pos=(0.0, 0.0, 0.0))
    request.addfinalizer(session.stop)
    lift = _named(logic, "lift")
    home_cells = set(brush_cells(lift, session.manager.cell_size))
    filed = lambda: {coord for coord, cell in session.manager.cells.items()  # noqa: E731
                     if any(b is lift for b in cell.brushes)}
    assert filed() == home_cells

    _tick(logic, 50)                                  # mid-motion
    travelled = bp.placed_box(lift)[:3]
    assert travelled[1] > 16.0 + 10.0
    session.tick(player_pos=(40000.0, 0.0, 40000.0))  # the player leaves
    assert lift.get('hidden') is True                 # parked
    registry = session.commit_all()
    records = [r for cell in registry.values() for r in cell.get('brushes', [])]
    assert not any(PLAY_KEYS & set(r) for r in records)

    _tick(logic, 20)                                  # time passes while parked
    while_parked = bp.placed_box(lift)[:3]
    session.tick(player_pos=(0.0, 0.0, 0.0))          # and comes back
    assert not lift.get('hidden')
    # One placement, owned by the table: the brush still says where it was
    # authored, Big World still files it at home, and it is where the table
    # has carried it.
    assert lift['pos'] == [300.0, 16.0, 0.0]
    assert filed() == home_cells
    assert bp.placed_box(lift)[:3] == while_parked
    table = logic.mover_runtime._movers()
    row = table.movers.row_of_obj[id(lift)]
    assert while_parked == tuple(table.movers.pos[row].tolist())


# ---------------------------------------------------------------------------
# The editor's mover preview
# ---------------------------------------------------------------------------

def test_the_mover_preview_drives_a_runtime_transform(main_window):
    lift = box_brush("lift", (300, 16, 0), (128, 32, 128), is_mover=True,
                     speed=96.0, distance=256.0, direction=[0, 1, 0])
    spinner = box_brush("fan", (-300, 64, 0), (256, 16, 32), is_mover=True,
                        rotate=True, speed=90.0, rot_axis=[0.0, 1.0, 0.0])
    main_window.state.brushes.extend([lift, spinner])
    for brush, check in ((lift, lambda b: bp.placed_box(b)[1] > 16.0),
                         (spinner, lambda b: bp.runtime_spin(b) is not None)):
        before = _public(brush)
        main_window.start_mover_preview(brush)
        for _ in range(20):
            main_window._advance_mover_preview()
        assert check(brush), brush['name']
        assert _public(brush) == before and not PLAY_KEYS & set(brush), brush['name']
        main_window.stop_mover_preview()
        assert bp.runtime_of(brush) is None, brush['name']
        assert bp.placed_box(brush) == bp.authored_box(brush)
    assert not len(bp.PREVIEW)


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

def test_the_plugin_accessor_reads_the_table(logic):
    from plugins.api import API_VERSION, placement
    assert API_VERSION == "1.8.0"
    _tick(logic, 30)
    lift = _named(logic, "lift")
    p = placement(lift)
    assert p.authored == (300.0, 16.0, 0.0)
    assert p.centre == bp.placed_box(lift)[:3] and p.centre != p.authored
    assert p.spin is None
    assert p.bounds == bp.world_bounds(lift)
    fan = placement(_named(logic, "fan"))
    assert fan.spin is not None and fan.spin[3] > 0.0


def test_the_deathmatch_navigation_sees_a_mover_where_it_is(logic):
    from plugins.deathmatch.moving_navigation import NavigationWorld, live_centre
    _tick(logic, 30)
    lift = _named(logic, "lift")
    nav = NavigationWorld(logic)
    views = {b['name']: b for b in nav.brushes()}
    assert views['lift']['pos'] == list(live_centre(lift))
    assert views['lift'] is not lift and lift['pos'] == [300.0, 16.0, 0.0]
    index = logic.editor_state.brushes.index(lift)
    stops = nav.stops(index, lift)
    assert stops[0] == live_centre(lift)
    assert (300.0, 16.0, 0.0) in stops                 # its authored rest


# ---------------------------------------------------------------------------
# The fast paths say what the plain ones say
# ---------------------------------------------------------------------------

def _crowd():
    """Enough movers and doors for the planned (vectorised) passes: linear,
    spinning and still movers, doors, and one brush that is both."""
    brushes = [box_brush("ground", (0, -16, 0), (8192, 32, 8192))]
    for k in range(14):
        brushes.append(box_brush(
            "m%d" % k, (k * 200.0, 32.0, 0.0), (64, 64, 64), is_mover=True,
            start_on=k % 5 != 4, rotate=k % 3 == 0, speed=48.0 + k,
            distance=0 if k % 7 == 6 else 160.0, direction=[0, 1, 0],
            rot_axis=[0.0, 1.0, 0.0] if k % 2 else [0.3, 1.0, 0.2]))
    for k in range(14):
        brushes.append(box_brush(
            "d%d" % k, (k * 200.0, 32.0, 600.0), (64, 96, 16), is_door=True,
            door_speed=96.0 + k, door_distance=128.0, door_direction="up",
            open_time=0.4))
    brushes.append(box_brush(
        "both", (-400.0, 32.0, 0.0), (64, 64, 64), is_mover=True, is_door=True,
        start_on=True, rotate=True, speed=40.0, distance=96.0,
        direction=[1, 0, 0], door_speed=64.0, door_distance=64.0,
        door_direction="up", open_time=0.4))
    angled = angled_brush("ramp", (0, 40, -400), (128, 64, 128),
                          clip_normal=(0.3, 1.0, 0.0), is_mover=True, start_on=True,
                          rotate=True, speed=64.0, distance=192.0, direction=[1, 0, 0])
    brushes.append(angled)
    return brushes


@pytest.fixture
def crowd(request):
    logic = _playing(_crowd())
    request.addfinalizer(logic.stop)
    # Half the doors open (the rest stay shut, at rest), and the twin.
    opened = {"d%d" % k for k in range(0, 14, 2)} | {"both"}
    for i, brush in enumerate(logic.editor_state.brushes):
        if brush['name'] in opened:
            logic.mover_runtime._trigger_door_open(i, brush)
    return logic


def test_publication_is_the_same_row_by_row_or_in_bulk(crowd):
    _tick(crowd, 37)
    table = crowd.mover_runtime._movers()
    assert table.movers.any_twin and table.doors.any_twin
    for group in (table.movers, table.doors):
        rows = list(range(len(group.brushes)))
        by_row, in_bulk = {}, {}
        for row in rows:
            table._publish_row(group, row, by_row)
        table._publish(group, np.asarray(rows), entries=in_bulk)
        assert in_bulk.keys() == by_row.keys()
        for key, (brush, transform) in by_row.items():
            assert in_bulk[key][0] is brush
            assert in_bulk[key][1] == transform, brush['name']
            assert all(type(v) is float for v in in_bulk[key][1]), brush['name']
        # Every row is in one of three states, and the published entries
        # say exactly which: turned, displaced, or at rest (no entry).
        states = {brush['name']: (brush['name'] in {b['name'] for b, _ in by_row.values()})
                  for brush in group.brushes}
        assert any(states.values()) and not all(states.values())
        # And the table's own entries are the same values.
        for key, entry in by_row.items():
            if key in table.runtime_entries:
                assert table.runtime_entries[key] == entry


def _same_rows(rows, fresh, names=('lo32', 'hi32', 'lo', 'hi')):
    for name in names:
        assert np.array_equal(getattr(rows, name), getattr(fresh, name)), name


def test_collision_rows_follow_movers_and_skip_when_nothing_moved(crowd, monkeypatch):
    """An unchanged query does no work per mover -- the rows are not even
    gathered -- because the resolver's placement stamps say nothing moved; a
    tick of movement and a journaled editor edit are followed, bit for bit."""
    from engine.logic_collision import CollisionRows
    brushes = crowd.editor_state.brushes
    rows = CollisionRows(brushes, 1)
    rows.refresh_movers()
    for ticks in (1, 13, 40):
        _tick(crowd, ticks)
        rows.refresh_movers()
        _same_rows(rows, CollisionRows(brushes, 1))
    gathers = {'n': 0}
    real_boxes = bp.BulkBoxes.boxes

    def boxes(bulk):
        gathers['n'] += 1
        return real_boxes(bulk)

    monkeypatch.setattr(bp.BulkBoxes, 'boxes', boxes)
    before = rows.lo.copy()
    for _ in range(100):
        rows.refresh_movers()                   # nothing moved since
    assert gathers['n'] == 0
    assert np.array_equal(rows.lo, before)
    _tick(crowd, 1)
    rows.refresh_movers()                       # one gather, then quiet again
    rows.refresh_movers()
    assert gathers['n'] == 1
    _same_rows(rows, CollisionRows(brushes, 1))
    # An editor edit of a placement is journaled (mark_world_changed, as the
    # console's setprop does), and followed, in place or as a new list.
    still = next(b for b in rows.movers if bp.runtime_of(b) is None)
    still['pos'][1] += 64.0
    crowd.editor_state.mark_world_changed([still])
    rows.refresh_movers()
    _same_rows(rows, CollisionRows(brushes, 1))
    still['size'] = [v * 2.0 for v in still['size']]
    crowd.editor_state.mark_world_changed([still])
    rows.refresh_movers()
    _same_rows(rows, CollisionRows(brushes, 1))


def test_the_physics_rows_skip_an_unchanged_query_too(crowd, monkeypatch):
    """The physics grid's rows (queried by the logic and the AI threads)
    follow the same stamps: no work per mover while nothing moves."""
    brushes = crowd.editor_state.brushes
    grid = SpatialGrid()
    grid.populate(brushes)
    rows = grid._cell_rows
    rows.refresh_movers()

    def fresh_rows():
        other = SpatialGrid()
        other.populate(brushes)
        other._cell_rows.refresh_movers()
        return other._cell_rows

    gathers = {'n': 0}
    real_boxes = bp.BulkBoxes.boxes

    def boxes(bulk):
        gathers['n'] += 1
        return real_boxes(bulk)

    monkeypatch.setattr(bp.BulkBoxes, 'boxes', boxes)
    for _ in range(100):
        rows.refresh_movers()
    assert gathers['n'] == 0
    _tick(crowd, 7)
    rows.refresh_movers()
    rows.refresh_movers()
    assert gathers['n'] == 1
    _same_rows(rows, fresh_rows(), ('lo', 'hi'))
    still = next(b for _row, b in rows.movers if bp.runtime_of(b) is None)
    still['pos'] = [still['pos'][0], still['pos'][1] + 32.0, still['pos'][2]]
    crowd.editor_state.mark_world_changed([still])
    rows.refresh_movers()
    _same_rows(rows, fresh_rows(), ('lo', 'hi'))


@pytest.mark.parametrize('scalar_below', [0, 10 ** 6], ids=['by-columns', 'by-brush'])
def test_bulk_boxes_are_world_box_from_the_tables_columns(crowd, monkeypatch, scalar_below):
    """The movers' world boxes gathered from the mover table's columns
    (brush_placement.BulkBoxes) are world_box brush by brush, to the bit --
    spinners, a brush that is both mover and door, an angled mover, rows at
    rest -- including when an earlier runtime source (the editor preview)
    answers for a brush the table also holds, after a saved-game restore,
    after an authored edit in play, and after stop. A gather asks no brush
    for its runtime transform. Both ways of gathering: by the table's
    columns, and (a short list) brush by brush from the runtime entries."""
    monkeypatch.setattr(bp.BulkBoxes, 'SCALAR_BELOW', scalar_below)
    table = crowd.mover_runtime._movers()
    movers = [b for b in crowd.editor_state.brushes
              if b.get('is_mover') or b.get('is_door')]
    bulk = bp.BulkBoxes(movers)

    def check(label):
        centre, half = bulk.boxes()
        for i, brush in enumerate(movers):
            want = bp.world_box(brush)
            got = tuple(centre[i].tolist()) + tuple(half[i].tolist())
            assert got == want, (label, brush['name'])

    check('at rest')
    for ticks in (1, 9, 31):
        _tick(crowd, ticks)
        check('after %d ticks' % ticks)
    lookups = {'n': 0}
    real = bp.runtime_of
    monkeypatch.setattr(bp, 'runtime_of', lambda b: lookups.__setitem__('n', lookups['n'] + 1) or real(b))
    bulk.boxes()
    assert lookups['n'] == 0
    monkeypatch.setattr(bp, 'runtime_of', real)
    spinner = next(b for b in movers if b['name'] == 'm3')
    bp.PREVIEW.set(spinner, offset=(0.0, 500.0, 0.0), spin=((1.0, 0.0, 0.0), 45.0))
    check('preview over the table')
    bp.PREVIEW.clear()
    table.restore_placement(spinner, (600.0, 90.0, 0.0), 77.0)
    check('restored')
    door = next(b for b in movers if b['name'] == 'd2')
    door['pos'] = [door['pos'][0] + 16.0] + list(door['pos'][1:])
    crowd.editor_state.mark_world_changed([door])
    _tick(crowd, 1)
    check('authored edit in play')
    table.reset()
    check('stopped')


def test_every_runtime_source_stamps_its_changes(crowd):
    """The O(1) skip is only sound if every change to a runtime transform
    moves the stamp: the mover table's ticks, saved-game restores and stop,
    and every RuntimeOverrides set and clear (the editor preview, tests)."""
    table = crowd.mover_runtime._movers()
    lift = next(b for b in crowd.editor_state.brushes if b['name'] == 'm0')
    stamp = lambda: bp.runtime_generation   # noqa: E731
    for change in (lambda: _tick(crowd, 1),
                   lambda: table.restore_placement(lift, (5.0, 60.0, 0.0), 10.0),
                   lambda: table.move_to(lift, (5.0, 70.0, 0.0)),
                   lambda: bp.PREVIEW.set(lift, offset=(0.0, 8.0, 0.0)),
                   lambda: bp.PREVIEW.clear(lift),
                   lambda: table.reset()):
        before = stamp()
        change()
        assert stamp() != before
    before = stamp()
    bp.world_bounds(lift)                       # reading moves nothing
    assert stamp() == before


def test_stopping_and_rebuilding_rows_swaps_the_transforms_whole(crowd):
    _tick(crowd, 25)
    table = crowd.mover_runtime._movers()
    before = dict(table.runtime_entries)
    assert before
    seen = []
    real = table._publish

    def watching(group, rows, entries=None, here=None):
        # A reader during the rebuild still sees every transform it saw.
        seen.append(dict(table.runtime_entries) == before)
        return real(group, rows, entries, here)

    table._publish = watching
    try:
        table._adopt(table.movers)
    finally:
        del table._publish
    assert seen and all(seen)
    assert table.runtime_entries == before


def test_stopping_play_with_a_brush_that_is_both_returns_it_home(crowd):
    """Stop empties the movers' rows before the doors' (logic_session): the
    doors' rows, republished as the movers' are rebuilt, must not reach for
    the twin's spin in rows that are gone (Stage 1 stopped such a level;
    Stage 2b raised IndexError)."""
    both = _named(crowd, "both")
    homes = [bp.authored_box(b)[:3] for b in crowd.editor_state.brushes]
    _tick(crowd, 40)
    assert bp.runtime_of(both) is not None
    crowd.mover_runtime._reset_movers()
    crowd.mover_runtime._reset_doors()
    for brush, home in zip(crowd.editor_state.brushes, homes):
        assert bp.placed_box(brush)[:3] == home, brush['name']
        assert bp.runtime_spin(brush) is None, brush['name']
    assert not crowd.mover_runtime._movers().runtime_entries
