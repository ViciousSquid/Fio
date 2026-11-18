"""Latched editor look uses the real view/input/camera path without a button."""

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from editor.things import PlayerStart
from tests.helpers.worlds import level_data, make_thing, room

pytestmark = pytest.mark.qt


class FakeCursor:
    """The OS cursor, under the test's control (see _poll_mouse_look)."""

    def __init__(self):
        self.pos = QPoint()

    def set_pos(self, *args):
        self.pos = QPoint(*args) if len(args) == 2 else QPoint(args[0])


@pytest.fixture
def editing(fio_session, monkeypatch):
    import engine.qt_game_view as gv
    session = fio_session(level_data(
        brushes=room(), things=[make_thing(PlayerStart, 'start', (0, 8, 0))]))
    cursor = FakeCursor()
    monkeypatch.setattr(gv.QCursor, 'setPos', cursor.set_pos)
    monkeypatch.setattr(gv.QCursor, 'pos', lambda: QPoint(cursor.pos))
    # Polled look turns only in the active window; offscreen has none.
    monkeypatch.setattr(type(session.window), 'isActiveWindow', lambda self: True)
    session.view._mac_mouse = None
    session.cursor = cursor
    session.view.setFocus()
    return session


def press(widget, key=Qt.Key_Z, modifiers=Qt.NoModifier, repeat=False):
    QApplication.sendEvent(widget, QKeyEvent(
        QEvent.KeyPress, key, modifiers, 'z' if key == Qt.Key_Z else '', repeat))


def button(view, kind, down):
    view_event = QMouseEvent(kind, view.rect().center(), Qt.RightButton,
                             Qt.RightButton if down else Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view, view_event)


def test_z_from_a_2d_view_toggles_look_once_per_physical_press(editing):
    view = editing.view
    press(editing.window.view_top)
    assert view.editor_freelook_active and view.mouselook_active
    assert view.hasFocus()
    assert view.cursor().shape() == Qt.BlankCursor
    assert view.mouseGrabber() is view
    press(view, repeat=True)
    QApplication.sendEvent(view, QKeyEvent(QEvent.KeyRelease, Qt.Key_Z, Qt.NoModifier))
    assert view.editor_freelook_active
    press(view)
    assert not view.editor_freelook_active and not view.mouselook_active
    assert view.cursor().shape() == Qt.ArrowCursor
    assert view.mouseGrabber() is not view


def test_mouse_without_buttons_rotates_the_editor_camera_only_while_enabled(editing):
    view, logic, cursor = editing.view, editing.logic, editing.cursor
    press(view)
    centre = view.mapToGlobal(view.rect().center())
    view._poll_mouse_look()     # the capture re-centre lands
    camera = logic.camera.get_editor_camera()
    yaw, pitch = camera.yaw, camera.pitch
    cursor.set_pos(centre + QPoint(10, 4))
    view._poll_mouse_look()
    logic._step_frame(1 / 60)
    assert (camera.yaw, camera.pitch) != (yaw, pitch)
    assert cursor.pos == centre
    press(view)
    yaw, pitch = camera.yaw, camera.pitch
    cursor.set_pos(centre + QPoint(20, 8))
    view._poll_mouse_look()
    logic._step_frame(1 / 60)
    assert (camera.yaw, camera.pitch) == (yaw, pitch)


def test_right_button_and_toggle_have_independent_lifetimes(editing):
    view = editing.view
    button(view, QEvent.MouseButtonPress, True)
    assert view.mouselook_active and not view.editor_freelook_active
    button(view, QEvent.MouseButtonRelease, False)
    assert not view.mouselook_active
    press(view)
    button(view, QEvent.MouseButtonPress, True)
    button(view, QEvent.MouseButtonRelease, False)
    assert view.mouselook_active and view.editor_freelook_active
    button(view, QEvent.MouseButtonPress, True)
    press(view)  # unlatch while the right button is still held
    assert view.mouselook_active and not view.editor_freelook_active
    assert view.mouseGrabber() is view
    button(view, QEvent.MouseButtonRelease, False)
    assert not view.mouselook_active


@pytest.mark.parametrize('exit_kind', ['escape', 'focus_loss'])
def test_release_frees_the_cursor_and_does_not_clear_selection(editing, exit_kind):
    view, window = editing.view, editing.window
    selection = editing.state.brushes[0]
    window.set_selected_objects([selection])
    press(view)
    if exit_kind == 'escape':
        press(view, Qt.Key_Escape)
    else:
        window.keys_pressed.add(Qt.Key_W)
        editing.game_state.set_keys({Qt.Key_W})
        QApplication.sendEvent(view, QFocusEvent(QEvent.FocusOut, Qt.ActiveWindowFocusReason))
        assert not window.keys_pressed and not editing.game_state.get_keys()
    assert not view.editor_freelook_active and not view.mouselook_active
    assert view.mouseGrabber() is not view
    assert view.cursor().shape() == Qt.ArrowCursor
    assert editing.state.selected_objects == [selection]


def test_play_entry_clears_editor_look_and_z_is_left_to_the_game(editing):
    press(editing.view)
    editing.start_play()
    assert not editing.view.mouselook_active and not editing.view.editor_freelook_active
    press(editing.view)
    assert not editing.view.editor_freelook_active
    editing.stop_play()
    assert not editing.view.mouselook_active


def test_typing_and_modified_z_do_not_start_freelook(editing):
    search = editing.window.scene_hierarchy.search_box
    search.setFocus()
    QTest.keyClick(search, Qt.Key_Z)
    assert search.text() == 'z'
    assert not editing.view.editor_freelook_active
    for modifier_key, modifier in [(Qt.Key_Control, Qt.ControlModifier),
                                   (Qt.Key_Shift, Qt.ShiftModifier)]:
        QTest.keyPress(editing.view, modifier_key)
        QTest.keyClick(editing.view, Qt.Key_Z, modifier)
        QTest.keyRelease(editing.view, modifier_key)
    assert not editing.view.editor_freelook_active


def _turns(editing):
    """Whether a mouse movement turns the editor camera now."""
    view, logic, cursor = editing.view, editing.logic, editing.cursor
    centre = view.mapToGlobal(view.rect().center())
    view._poll_mouse_look()             # a capture re-centre may land first
    camera = logic.camera.get_editor_camera()
    before = (camera.yaw, camera.pitch)
    cursor.set_pos(centre + QPoint(12, 5))
    view._poll_mouse_look()
    logic._step_frame(1 / 60)
    return (camera.yaw, camera.pitch) != before


def _right_physically(monkeypatch, down):
    import engine.qt_game_view as gv
    monkeypatch.setattr(gv.QApplication, 'mouseButtons',
                        staticmethod(lambda: Qt.RightButton if down else Qt.NoButton))


def test_right_drag_keeps_turning_when_a_panel_takes_the_focus(editing, monkeypatch):
    """Focus moving to another widget of the window mid-drag (a panel being
    rebuilt, a tool window) does not end the drag while the button is down."""
    view = editing.view
    _right_physically(monkeypatch, True)
    button(view, QEvent.MouseButtonPress, True)
    assert view.mouselook_active and view.hasFocus()
    search = editing.window.scene_hierarchy.search_box
    search.setFocus(Qt.OtherFocusReason)
    QApplication.sendEvent(view, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
    assert view.mouselook_active and view.mouseGrabber() is view
    assert _turns(editing), "the held right button stopped turning the camera"
    button(view, QEvent.MouseButtonRelease, False)
    assert not view.mouselook_active


def test_a_z_latch_ends_on_focus_loss_but_a_held_drag_goes_on(editing, monkeypatch):
    view = editing.view
    _right_physically(monkeypatch, True)
    press(view)                                         # latched
    button(view, QEvent.MouseButtonPress, True)         # and held
    QApplication.sendEvent(view, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
    assert not view.editor_freelook_active and view.mouselook_active
    button(view, QEvent.MouseButtonRelease, False)
    assert not view.mouselook_active


def test_focus_loss_with_the_button_up_ends_the_look(editing, monkeypatch):
    """A drag whose button is no longer down ends with the focus, as before."""
    view = editing.view
    _right_physically(monkeypatch, False)
    button(view, QEvent.MouseButtonPress, True)
    QApplication.sendEvent(view, QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
    assert not view.mouselook_active and view.mouseGrabber() is not view


def test_a_release_that_went_elsewhere_does_not_leave_the_look_stuck(editing):
    view = editing.view
    button(view, QEvent.MouseButtonPress, True)
    assert view.mouselook_active
    move = QMouseEvent(QEvent.MouseMove, view.rect().center() + QPoint(3, 0),
                       Qt.NoButton, Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(view, move)                  # the button is up
    assert not view.mouselook_active and view.mouseGrabber() is not view


def test_a_right_press_takes_the_focus_for_wasd(editing):
    view = editing.view
    editing.window.scene_hierarchy.search_box.setFocus()
    assert not view.hasFocus()
    button(view, QEvent.MouseButtonPress, True)
    assert view.hasFocus() and view.mouselook_active
    button(view, QEvent.MouseButtonRelease, False)


def _window_active(monkeypatch, editing, active, app_active=True):
    import engine.qt_game_view as gv
    monkeypatch.setattr(type(editing.window), 'isActiveWindow', lambda self: active)
    state = Qt.ApplicationActive if app_active else Qt.ApplicationInactive
    monkeypatch.setattr(gv.QApplication, 'applicationState', staticmethod(lambda: state))


def test_right_drag_turns_when_another_fio_window_is_active(editing, monkeypatch):
    """After the colour picker (or with a floating panel active) the keys
    still reach the editor but the view's window is not the active one; on
    macOS a right-click does not activate it. Holding the look, it turns."""
    view = editing.view
    _window_active(monkeypatch, editing, False)
    button(view, QEvent.MouseButtonPress, True)
    assert view.mouselook_active and view.mouseGrabber() is view
    assert _turns(editing), "right-drag was dead with another Fio window active"
    button(view, QEvent.MouseButtonRelease, False)


def test_another_application_in_front_stops_the_look(editing, monkeypatch):
    view = editing.view
    _window_active(monkeypatch, editing, False, app_active=False)
    button(view, QEvent.MouseButtonPress, True)
    assert not _turns(editing)
    button(view, QEvent.MouseButtonRelease, False)


def test_a_right_press_activates_the_views_window(editing, monkeypatch):
    view = editing.view
    activated = []
    _window_active(monkeypatch, editing, False)
    monkeypatch.setattr(type(editing.window), 'activateWindow',
                        lambda self: activated.append(self))
    button(view, QEvent.MouseButtonPress, True)
    assert activated == [editing.window]
    button(view, QEvent.MouseButtonRelease, False)


def test_a_look_that_cannot_turn_says_why_once(editing, monkeypatch):
    import engine.qt_game_view as gv
    logged = []
    monkeypatch.setattr(gv, 'debug_log', lambda category, message: logged.append(message))
    view = editing.view
    _window_active(monkeypatch, editing, False, app_active=False)
    button(view, QEvent.MouseButtonPress, True)
    view._poll_mouse_look()
    view._poll_mouse_look()
    assert len(logged) == 1 and "app active=False" in logged[0]
    button(view, QEvent.MouseButtonRelease, False)
