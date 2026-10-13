"""Selecting, dragging and nudging placed terrain stamps in the 3D view."""

import pytest

pytest.importorskip("PyQt5", reason="the 3D view is editor-tier")

pytestmark = pytest.mark.qt


@pytest.fixture
def stamp_view(main_window, tmp_path, monkeypatch):
    from PyQt5.QtGui import QColor, QImage
    from engine.terrain import Terrain

    texture = str(tmp_path / "rock.png")
    image = QImage(16, 16, QImage.Format_RGBA8888)
    image.fill(QColor(200, 100, 50, 255))
    assert image.save(texture)

    terrain = Terrain(seed=1)
    main_window.terrain = terrain
    a = terrain.add_paint_stamp(0.0, 0.0, 16.0, texture, feather=0.0)
    b = terrain.add_paint_stamp(200.0, 0.0, 16.0, texture, feather=0.0)
    main_window.state.terrain_data = terrain.to_dict()

    view = main_window.view_3d
    view.terrain_sculpt_mode = 'stamp'
    view.set_terrain_sculpt_active(True)
    cursor = {'hit': (0.0, 0.0, 0.0)}
    monkeypatch.setattr(view, 'raycast_terrain', lambda mx, my: cursor['hit'])
    yield view, terrain, a, b, cursor
    view.set_terrain_sculpt_active(False)


def _mouse(view, kind, shift=False):
    from PyQt5.QtCore import QEvent, QPoint, Qt
    from PyQt5.QtGui import QMouseEvent
    mods = Qt.ShiftModifier if shift else Qt.NoModifier
    etype = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
             'release': QEvent.MouseButtonRelease}[kind]
    buttons = Qt.NoButton if kind == 'release' else Qt.LeftButton
    event = QMouseEvent(etype, QPoint(50, 50), Qt.LeftButton, buttons, mods)
    getattr(view, {'press': 'mousePressEvent', 'move': 'mouseMoveEvent',
                   'release': 'mouseReleaseEvent'}[kind])(event)


def _key(main_window, key, shift=False):
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    mods = Qt.ShiftModifier if shift else Qt.NoModifier
    return main_window.eventFilter(
        main_window.view_3d, QKeyEvent(QEvent.KeyPress, key, mods))


def test_shift_click_selects_and_tints_a_stamp(stamp_view):
    view, terrain, a, b, cursor = stamp_view
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    assert terrain.selected_stamp_ids == {a['id']}
    assert terrain._paint_display                     # drawn tinted
    assert len(terrain.paint_stamps) == 2             # no new stamp placed

    cursor['hit'] = (200.0, 0.0, 0.0)
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    assert terrain.selected_stamp_ids == {a['id'], b['id']}

    _mouse(view, 'press', shift=True)                 # shift-click again: off
    _mouse(view, 'release', shift=True)
    assert terrain.selected_stamp_ids == {a['id']}


def test_dragging_a_selected_stamp_moves_it_as_one_undo_step(main_window, stamp_view):
    view, terrain, a, b, cursor = stamp_view
    _mouse(view, 'press', shift=True)
    for x in (10.0, 30.0, 64.0):
        cursor['hit'] = (x, 0.0, 8.0)
        _mouse(view, 'move')
    _mouse(view, 'release')
    assert (a['x'], a['z']) == (64.0, 8.0)
    assert (b['x'], b['z']) == (200.0, 0.0)
    saved = main_window.state.terrain_data['paint_stamps']
    assert saved[0]['x'] == 64.0 and saved[0]['z'] == 8.0

    main_window.undo()
    restored = main_window.terrain.paint_stamps
    assert (restored[0]['x'], restored[0]['z']) == (0.0, 0.0)


def test_arrow_keys_nudge_selected_stamps_like_brushes(main_window, stamp_view):
    from PyQt5.QtCore import Qt
    view, terrain, a, b, cursor = stamp_view
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    grid = main_window.right_tabs.currentWidget().grid_size

    assert _key(main_window, Qt.Key_Right)
    assert (a['x'], a['z']) == (grid, 0.0)
    assert _key(main_window, Qt.Key_Down, shift=True)
    assert (a['x'], a['z']) == (grid, 5 * grid)
    assert _key(main_window, Qt.Key_Up)
    assert (a['x'], a['z']) == (grid, 4 * grid)
    assert (b['x'], b['z']) == (200.0, 0.0)
    main_window._finish_stamp_nudge()
    assert main_window.state.terrain_data['paint_stamps'][0]['x'] == grid


def test_escape_clears_the_selection_before_leaving_the_brush(stamp_view):
    from PyQt5.QtCore import QEvent, Qt
    from PyQt5.QtGui import QKeyEvent
    view, terrain, a, b, cursor = stamp_view
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not terrain.selected_stamp_ids and not terrain._paint_display
    assert view.terrain_sculpt_active


def test_shift_click_on_bare_terrain_deselects(stamp_view):
    view, terrain, a, b, cursor = stamp_view
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    cursor['hit'] = (600.0, 0.0, 600.0)
    _mouse(view, 'press', shift=True)
    _mouse(view, 'release', shift=True)
    assert not terrain.selected_stamp_ids
    assert len(terrain.paint_stamps) == 2
