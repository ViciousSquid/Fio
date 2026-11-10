"""M4: the mover table writes a render table only the rows that moved.

``MoverTable.publish`` used to write every mover's and door's row into the
render table every frame, moved or not, and the renderer found the changes
by comparing columns. Now the tick's own record of what it published (each
row passed to ``_publish`` / ``_publish_row``, the same publication every
gameplay reader relies on) decides what is written, and every write is
logged by the table (``RenderTable.transforms_since``). These tests pin:

* only rows that moved are written; a world at rest writes none;
* the two render buffers' tables take turns, and each catches up on what
  moved during the other's frame;
* every path that moves a mover leaves the table where ``placed_box`` puts
  it: the tick, door activation, a saved game's restore, Big World parking
  and waking, stopping and restarting play;
* a pose written without being published -- a writer outside the record --
  is caught by the oracle (``change_detection.ORACLE``).
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the logic thread pulls in editor.things")

from engine import brush_placement as bp                           # noqa: E402
from engine import change_detection                                 # noqa: E402
from engine.render_table import RenderTable                        # noqa: E402
from tests.brush_model.test_runtime_transform import (               # noqa: E402
    _named, _playing, _tick, _world)

pytestmark = pytest.mark.qt


@pytest.fixture
def oracle(monkeypatch):
    monkeypatch.setattr(change_detection, 'ORACLE', True)
    monkeypatch.setattr(change_detection, 'RAISE', True)


@pytest.fixture
def logic(request):
    logic = _playing(_world())
    request.addfinalizer(logic.stop)
    return logic


def _table(logic):
    table = RenderTable()
    table.sync(logic.editor_state.brushes, 1)
    return table


def _publish(logic, table):
    """Publish into *table*; the slots it was written (sorted, unique), or
    None when it was written whole."""
    version = table.transform_version
    logic.mover_runtime._movers().publish(logic, table)
    rows = table.transforms_since(version)
    return None if rows is None else np.unique(rows)


def _slots(logic, *names):
    brushes = logic.editor_state.brushes
    return sorted(brushes.index(_named(logic, name)) for name in names)


def _drawn_where_placed(logic, table):
    for slot, brush in enumerate(logic.editor_state.brushes):
        want = np.asarray(bp.placed_box(brush)[:3])
        assert np.array_equal(table.bounds[slot, :3], want), brush['name']
        spin = bp.runtime_spin(brush)
        angle = 0.0 if spin is None else spin[3]
        assert float(table.rot[slot, 3]) == pytest.approx(angle), brush['name']


@pytest.fixture
def marks_mode(monkeypatch):
    """Rows picked out one by one, whatever the group's size (dense mode
    writes small groups whole)."""
    from engine import mover_table
    monkeypatch.setattr(mover_table, 'DENSE_SLACK_ROWS', -1)
    monkeypatch.setattr(mover_table, 'DENSE_SHARE', 2.0)


def test_only_the_rows_that_moved_are_written(logic, oracle, marks_mode):
    table = _table(logic)
    _tick(logic, 5)
    assert _publish(logic, table) is not None
    _tick(logic, 1)
    # The lift, the fan and the angled lift move every tick; the gate is
    # shut and the ground never moves.
    assert _publish(logic, table).tolist() == _slots(
        logic, 'lift', 'fan', 'ramp_lift')
    _drawn_where_placed(logic, table)


@pytest.mark.parametrize("mode", ["dense", "marks"])
def test_a_world_at_rest_writes_nothing(logic, oracle, monkeypatch, mode):
    from engine import mover_table
    if mode == "marks":
        monkeypatch.setattr(mover_table, 'DENSE_SLACK_ROWS', -1)
        monkeypatch.setattr(mover_table, 'DENSE_SHARE', 2.0)
    table = _table(logic)
    gate = _named(logic, 'gate')
    logic.mover_runtime._trigger_door_open(logic.editor_state.brushes.index(gate), gate)
    _tick(logic, 2)
    _publish(logic, table)
    _tick(logic, 1)
    assert set(_slots(logic, 'lift', 'fan', 'gate', 'ramp_lift')) <= set(
        _publish(logic, table).tolist())
    # Stop the continuous movers; the gate finishes opening and holds.
    movers = logic.mover_runtime._movers()
    for name in ('lift', 'fan', 'ramp_lift'):
        row = movers.movers.row_of_obj[id(_named(logic, name))]
        movers.movers.start_on[row] = False
        movers.movers.rotate[row] = False
    _tick(logic, 120)
    _publish(logic, table)
    change_detection.DETECT.reset()
    assert _publish(logic, table).tolist() == []
    assert change_detection.DETECT.sites['mover.publish'][0] == 0
    _drawn_where_placed(logic, table)


def test_alternating_tables_each_catch_up(logic, oracle):
    tables = [_table(logic), _table(logic)]
    for frame in range(12):
        _tick(logic, 1 + frame % 3)          # catch-up frames of several ticks
        _publish(logic, tables[frame % 2])
        _drawn_where_placed(logic, tables[frame % 2])


def test_door_activation_is_written(logic, oracle):
    table = _table(logic)
    _tick(logic, 3)
    _publish(logic, table)
    gate = _named(logic, 'gate')
    logic.mover_runtime._trigger_door_open(logic.editor_state.brushes.index(gate), gate)
    _tick(logic, 10)
    assert logic.editor_state.brushes.index(gate) in _publish(logic, table).tolist()
    _drawn_where_placed(logic, table)


def test_a_restored_placement_is_written(logic, oracle):
    table = _table(logic)
    _tick(logic, 4)
    _publish(logic, table)
    lift = _named(logic, 'lift')
    logic.mover_runtime._movers().restore_placement(lift, (300.0, 120.0, 0.0))
    assert logic.editor_state.brushes.index(lift) in _publish(logic, table).tolist()
    _drawn_where_placed(logic, table)


def test_stopping_and_playing_again_rewrites_the_rows(logic, oracle):
    table = _table(logic)
    _tick(logic, 30)
    _publish(logic, table)
    logic.mover_runtime._reset_movers()
    logic.mover_runtime._reset_doors()
    logic.mover_runtime._init_movers()
    logic.mover_runtime._init_doors()
    _tick(logic, 7)
    assert _publish(logic, table) is not None
    _drawn_where_placed(logic, table)


def test_big_world_parks_and_wakes_a_mover_and_the_table_follows(request, oracle):
    from plugins.bigworld.runtime import BigWorldSession
    logic = _playing(_world())
    request.addfinalizer(logic.stop)
    session = BigWorldSession(logic, activation_radius=1024.0,
                              deactivation_radius=1200.0)
    session.start(player_pos=(0.0, 0.0, 0.0))
    request.addfinalizer(session.stop)
    tables = [_table(logic), _table(logic)]
    brushes = logic.editor_state.brushes
    frame = 0

    def frames(n):
        nonlocal frame
        for _ in range(n):
            _tick(logic, 1)
            table = tables[frame % 2]
            table.begin_frame(brushes, 1)            # the journal: park, wake
            _publish(logic, table)
            frame += 1

    frames(30)
    session.tick(player_pos=(40000.0, 0.0, 40000.0))     # parked
    frames(20)
    session.tick(player_pos=(0.0, 0.0, 0.0))             # woken
    frames(6)
    lift = _named(logic, 'lift')
    assert not lift.get('hidden')
    for table in tables:
        table.begin_frame(brushes, 1)
        _publish(logic, table)
        _drawn_where_placed(logic, table)


@pytest.mark.parametrize("mode", ["dense", "marks"])
def test_a_pose_written_without_being_published_is_caught(logic, oracle, monkeypatch, mode):
    from engine import mover_table
    if mode == "marks":
        monkeypatch.setattr(mover_table, 'DENSE_SLACK_ROWS', -1)
        monkeypatch.setattr(mover_table, 'DENSE_SHARE', 2.0)
    table = _table(logic)
    _tick(logic, 3)
    _publish(logic, table)
    movers = logic.mover_runtime._movers()
    row = movers.doors.row_of_obj[id(_named(logic, 'gate'))]
    movers.doors.pos[row, 1] += 40.0                     # no _publish: missed
    with pytest.raises(change_detection.MissedChange):
        _publish(logic, table)


@pytest.mark.parametrize("mode", ["dense", "marks"])
def test_both_publication_modes_leave_a_complete_record(logic, oracle, monkeypatch, mode):
    """Dense mode writes a whole group without picking rows out, but never
    around the table's versioning: every row of the group is written and
    logged (a superset of what moved), so the kept matrices and the instance
    streams downstream see every change. Marks mode logs exactly what moved.
    Either way the table ends up where placed_box puts every brush, and the
    rows logged cover every row that moved."""
    from engine import mover_table
    from engine.renderer.core.slot_transforms import SlotTransforms
    if mode == "dense":
        monkeypatch.setattr(mover_table, 'DENSE_SLACK_ROWS', 1 << 20)
    else:
        monkeypatch.setattr(mover_table, 'DENSE_SLACK_ROWS', -1)
        monkeypatch.setattr(mover_table, 'DENSE_SHARE', 2.0)
    tables = [_table(logic), _table(logic)]
    caches = [SlotTransforms(t) for t in tables]
    for cache, table in zip(caches, tables):
        cache.sync(table)
    group = logic.mover_runtime._movers().movers
    brushes = logic.editor_state.brushes
    for frame in range(10):
        _tick(logic, 1 + frame % 2)
        table, cache = tables[frame % 2], caches[frame % 2]
        before = {id(b): bp.placed_box(b) for b in brushes}
        logged = _publish(logic, table)
        cache.sync(table)                          # the oracle checks it
        _drawn_where_placed(logic, table)
        moved = {table.slot_of_id[b['id']] for b in group.brushes}
        if logged is not None:
            if mode == "dense":
                assert moved <= set(logged.tolist())   # the whole group
            else:
                assert set(logged.tolist()) <= moved | set(
                    table.slot_of_id[b['id']] for b in logic.mover_runtime._movers().doors.brushes)
        del before
