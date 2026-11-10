"""M4: every writer reports what it changed, and a missed report is caught.

The renderer no longer asks whether each row changed: it reads the change
records the tables publish (transform logs, row-list versions, immutable
visible sets). A writer that does not report what it wrote would leave the
renderer drawing stale data indefinitely, so every write path below runs in
the real editor (``FioTestSession``: MainWindow, the logic thread's
publication, the double-buffered render state, ``QtGameView.paintGL``) and
is checked by three oracles at every step:

1. **Oracle mode** (``change_detection.ORACLE``): every site that trusts a
   change record also does the full comparison the record replaced -- the
   kept matrices, the kept visible set, the passes' kept arrays, the
   instance streams' inputs, the mover publication, the row lists -- and
   raises ``MissedChange`` inside the frame.
2. **Debug Tables** (the built-in instrument, Debug -> Debug Tables): its
   copy of the published RenderTable equals a table rebuilt from scratch from
   the same world. This catches a writer that never told the table at all,
   which (1) cannot: (1) trusts the table.
3. **The picture**: the long-lived renderer draws exactly what the same
   renderer draws once every cache it keeps is dropped.

Write paths: editor edits (move, resize, retexture, add, delete), undo and
redo, the camera moving, mover and door activation in play, a saved game's
load, stopping play, renderer switching, Big World (a plugin) parking and
waking brushes. A test also forces a missed record, to show the harness
catches one.
"""

import os
import uuid

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                                    # noqa: E402

from editor.things import Light, PlayerStart                  # noqa: E402
from engine import brush_faces                                 # noqa: E402
from engine import change_detection                            # noqa: E402
from engine.render_table import RenderTable                    # noqa: E402
from tests.helpers.worlds import (                             # noqa: E402
    angled_brush, box_brush, level_data, make_thing, room)

pytestmark = [pytest.mark.gl, pytest.mark.integration]

_COLUMNS = ('bounds', 'rot', 'hidden', 'class_bits', 'colour', 'uv_scale',
            'uv_shift', 'uv_angle', 'uv_natural', 'uv_has_scale')


@pytest.fixture(autouse=True)
def oracle(monkeypatch):
    # Recorded, not raised: a miss found inside paintGL must not tear a GL
    # frame (and the process) down. Each check below reads the record.
    monkeypatch.setattr(change_detection, 'ORACLE', True)
    monkeypatch.setattr(change_detection, 'RAISE', False)


def _no_misses():
    misses = list(change_detection.MISSES)
    del change_detection.MISSES[:]
    assert not misses, misses[:3]


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0), angle=3.14159)
    lamp = make_thing(Light, "lamp", (0.0, 400.0, 0.0), radius=3000.0,
                      intensity=2.0, casts_shadows=False)
    brushes = room(size=2048.0, height=512.0) + [
        box_brush("crate", (-300.0, 64.0, -300.0), (128.0, 128.0, 128.0)),
        box_brush("pillar", (300.0, 128.0, -200.0), (64.0, 256.0, 64.0)),
        angled_brush("ramp", (0.0, 40.0, -500.0), (256.0, 80.0, 128.0)),
        box_brush("lift", (-500.0, 32.0, 200.0), (128.0, 32.0, 128.0),
                  is_mover=True, start_on=True, speed=96.0, distance=192.0,
                  direction=[0, 1, 0]),
        box_brush("fan", (500.0, 200.0, 200.0), (256.0, 16.0, 32.0),
                  is_mover=True, start_on=True, rotate=True, speed=90.0,
                  distance=0, rot_axis=[0.0, 1.0, 0.0]),
        box_brush("gate", (0.0, 64.0, 600.0), (192.0, 128.0, 16.0), is_door=True,
                  door_speed=128.0, door_distance=128.0, door_direction="up",
                  open_time=30.0),
    ]
    return level_data(brushes=brushes, things=[spawn, lamp])


def _named(session, name):
    return next(b for b in session.state.brushes if b.get('name') == name)


@pytest.fixture
def session(fio_session):
    session = fio_session(_level(), size=(200, 120))
    session.view.sysmon.set_active(False)
    return session


@pytest.fixture
def instrument(session):
    from tools.debug_tables import DebugTablesWindow
    window = DebugTablesWindow(session.window)
    window.timer.stop()
    yield window
    from PyQt5.QtCore import QCoreApplication, QEvent
    window.close()
    QCoreApplication.processEvents()        # its pending deferred restores
    window.main_window = None
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _drop_caches(renderer):
    """Everything the forward renderer keeps between frames for brushes."""
    renderer._release_brush_streams()
    renderer._slot_transform_caches.clear()
    renderer._pass_memos.clear()
    renderer._selected_payloads.clear()
    renderer._distance_culls.clear()


def _verify(session, instrument, picture=True):
    """The three oracles on the world as it is now."""
    session.publish()
    kept = session.paint()                     # (1) runs inside the frame
    _no_misses()
    # (2) Debug Tables' copy of the published table against a fresh one.
    instrument.refresh()
    shown = instrument.render
    with session.render_state() as frame:
        published = frame.render_table
        names = list(published.texture_names())
        ids = list(published.ids)
    fresh = RenderTable()
    fresh.sync(list(session.state.brushes), 1)
    fresh_names = fresh.texture_names()
    assert shown.count == fresh.count == len(session.state.brushes)
    rows = np.array([fresh.slot_of_id[i] for i in ids[:shown.count]], dtype=np.intp)
    assert np.array_equal(shown.hidden[:shown.count], fresh.hidden[rows])
    # A hidden row's transform is not drawn and not kept up to date (a parked
    # mover is neither walked nor published until it wakes, when it is
    # refreshed): transforms are compared on the shown rows.
    drawn = ~shown.hidden[:shown.count]
    for column in _COLUMNS:
        mine, theirs = getattr(shown, column)[:shown.count], getattr(fresh, column)[rows]
        if column in ('bounds', 'rot'):
            mine, theirs = mine[drawn], theirs[drawn]
        if not np.array_equal(mine, theirs):
            bad = np.flatnonzero((mine != theirs).reshape(len(mine), -1).any(axis=1))[:4]
            names = [b.get('name') for b in (fresh.brushes[i] for i in rows)]
            if column in ('bounds', 'rot'):
                names = [n for n, d in zip(names, drawn) if d]
            pytest.fail("published %s differs from a fresh table: %s" % (column, [
                (names[r], mine[r].tolist(), theirs[r].tolist()) for r in bad]))
    tex = np.asarray(shown.tex_name_id[:shown.count])
    assert [[names[t] if t >= 0 else None for t in row] for row in tex] == [
        [fresh_names[t] if t >= 0 else None for t in row] for row in fresh.tex_name_id[rows]]
    # (3) The same frame again, with every kept cache dropped.
    if picture:
        _drop_caches(session.view.renderer)
        assert np.array_equal(kept, session.paint()), "kept caches drew something else"
        _no_misses()
    return kept


def test_the_harness_catches_a_missed_record(session, instrument):
    _verify(session, instrument)
    row = len(session.state.brushes) - 2
    with session.render_state() as frame:
        table = frame.render_table
        table._seal(False)                     # a writer past the change log
        table.bounds[row, 0] += 64.0
        table._seal(True)
    session.paint()
    assert any('without being logged' in m for m in change_detection.MISSES)
    del change_detection.MISSES[:]
    with session.render_state() as frame:      # put it back, as it was
        table = frame.render_table
        table._seal(False)
        table.bounds[row, 0] -= 64.0
        table._seal(True)


def test_editor_edits_undo_and_redo(session, instrument):
    window, state = session.window, session.state
    _verify(session, instrument)
    crate, pillar = _named(session, 'crate'), _named(session, 'pillar')

    window.save_state()                                     # a move
    crate['pos'][0] += 96.0
    state.mark_world_changed([crate])
    _verify(session, instrument)

    window.save_state()                                     # a resize
    pillar['size'][1] *= 1.5
    state.mark_world_changed([pillar])
    _verify(session, instrument)

    window.save_state()                                     # a retexture
    brush_faces.set_face_material(crate, 'top', texture='Dev/256.jpg')
    state.mark_world_changed([crate])
    _verify(session, instrument)

    window.save_state()                                     # an addition
    state.brushes.append(box_brush("new", (200.0, 32.0, 400.0), (64.0, 64.0, 64.0)))
    state.mark_world_changed()
    _verify(session, instrument)

    window.save_state()                                     # a deletion
    state.brushes.remove(_named(session, 'pillar'))
    state.mark_world_changed()
    _verify(session, instrument)

    for _ in range(3):
        window.undo()
        _verify(session, instrument)
    for _ in range(2):
        window.redo()
        _verify(session, instrument)


def test_the_editor_camera_moving(session, instrument):
    camera = session.logic.camera.get_editor_camera()
    for step in range(4):
        camera.pos[0] += 150.0
        camera.yaw += 20.0
        _verify(session, instrument)
    _verify(session, instrument)                            # and standing still


def test_play_movers_doors_and_stopping(session, instrument):
    session.start_play()
    for _ in range(3):
        session.step(7, keys={Qt.Key_W})
        _verify(session, instrument)
    gate = _named(session, 'gate')
    session.logic.mover_runtime._trigger_door_open(session.state.brushes.index(gate), gate)
    for _ in range(3):
        session.step(9)
        _verify(session, instrument)
    session.stop_play()
    _verify(session, instrument)


def test_a_saved_game_loaded_in_play(session, instrument):
    session.start_play()
    name = "m4_%s" % uuid.uuid4().hex[:8]
    path = os.path.join(session.window.root_dir, "saves", name + ".fiosave")
    console = session.view.debug_console_window
    try:
        session.step(20)
        console.command_input.setText("save " + name)
        console._on_command_entered()
        session.step(45)
        _verify(session, instrument)
        console.command_input.setText("load " + name)
        console._on_command_entered()
        _verify(session, instrument)
        session.step(5)
        _verify(session, instrument)
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_renderer_switching(session, instrument, monkeypatch):
    from engine.renderer import registry
    from tests.helpers.renderers import FlatRenderer
    saved = dict(registry._factories)
    registry.register_renderer("Flat", FlatRenderer)
    try:
        _verify(session, instrument)
        assert session.view.switch_renderer("Flat")
        session.publish()
        session.paint()
        assert session.view.switch_renderer("Forward")
        _verify(session, instrument)
        _named(session, 'crate')['pos'][2] += 50.0
        session.state.mark_world_changed([_named(session, 'crate')])
        _verify(session, instrument)
    finally:
        registry._factories.clear()
        registry._factories.update(saved)


def test_big_world_parking_and_waking_a_plugin_change(session, instrument):
    from plugins.bigworld.runtime import BigWorldSession
    session.start_play()
    logic = session.logic
    big = BigWorldSession(logic, activation_radius=900.0, deactivation_radius=1000.0)
    big.start(player_pos=(0.0, 0.0, 0.0))
    try:
        session.step(5)
        _verify(session, instrument)
        big.tick(player_pos=(40000.0, 0.0, 40000.0))            # everything parks
        session.step(3)
        _verify(session, instrument)
        big.tick(player_pos=(0.0, 0.0, 0.0))                    # and wakes
        session.step(3)
        _verify(session, instrument)
    finally:
        big.stop()


def test_io_inputs_hide_show_and_tint(session, instrument):
    session.start_play()
    io = session.logic.io_manager
    crate = _named(session, 'crate')
    _verify(session, instrument)
    for name, parameter in (('Hide', ''), ('Show', ''), ('SetTint', '255 40 40'),
                            ('ToggleVisibility', ''), ('ToggleVisibility', '')):
        io._execute_input('crate', name, parameter, 'm4-test', target_id=crate['id'])
        session.step(2)
        _verify(session, instrument)


def test_a_writer_that_tells_no_one_is_caught_by_debug_tables(session, instrument):
    """The second oracle's reason to exist: a brush edited with no undo
    checkpoint, no world change and no journal entry -- a writer outside the
    contract -- leaves the published table stale, and only the comparison
    with a table rebuilt from the world sees it."""
    _verify(session, instrument)
    _named(session, 'crate')['pos'][0] += 200.0             # tells no one
    with pytest.raises(pytest.fail.Exception, match="differs from a fresh table"):
        _verify(session, instrument, picture=False)
