"""macOS: right-drag look in the 3D view after a Glow brush's colour change.

Reported on macOS: after picking a Glow brush's colour with the picker, holding
the right mouse button in the 3D view no longer turned the camera, while the
keyboard went on working; a plain brush's tint did not do it. This drives the
real thing on a real window system: a right-drag through the macOS input queue
(CGEventPost), before and after the Glow Color button's colour dialog, logging
which window is active, who has focus and the Debug Console's dead-look line.

``test_control_with_the_old_active_window_check`` repeats it with the check
the look used before (the view's own window must be the active one) and only
reports what it sees, so the two runs together say whether that was the cause.

Runs only with ``FIO_NATIVE_INPUT=1`` on macOS (play-click-native.yml).
"""

import ctypes
import os
import time

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtGui import QColor, QGuiApplication
from PyQt5.QtWidgets import QApplication, QColorDialog

from tests.native.test_play_click_native import (  # noqa: F401  (fixtures too)
    _activate_macos_app, _cg, in_view_point, macos_first_window, move_pointer,
    platform_name, pump, pump_until, warm_up_input)

pytestmark = [
    pytest.mark.qt,
    pytest.mark.gl,
    pytest.mark.skipif(os.environ.get('FIO_NATIVE_INPUT') != '1',
                       reason='moves the real pointer; set FIO_NATIVE_INPUT=1'),
]

kCGEventRightMouseDown, kCGEventRightMouseUp, kCGEventRightMouseDragged = 3, 4, 7
kCGMouseEventDeltaX, kCGMouseEventDeltaY = 4, 5


def _post_right_drag(steps=12, dx=20, dy=6):
    """Right button down, *steps* drags of (dx, dy) each, right button up --
    through the macOS input queue, at the pointer."""
    cg, cf = _cg()
    source = cg.CGEventSourceCreate(1)              # HID system state
    cg.CGEventSourceSetLocalEventsSuppressionInterval(source, 0.0)
    probe = cg.CGEventCreate(None)
    where = cg.CGEventGetLocation(probe)
    cf.CFRelease(probe)

    def post(kind, delta=(0, 0)):
        event = cg.CGEventCreateMouseEvent(source, kind, where, 1)    # button: right
        cg.CGEventSetIntegerValueField(event, kCGMouseEventDeltaX, delta[0])
        cg.CGEventSetIntegerValueField(event, kCGMouseEventDeltaY, delta[1])
        cg.CGEventPost(0, event)                    # HID event tap
        cf.CFRelease(event)

    post(kCGEventRightMouseDown)
    return post, source, cf


class RightPressCounter(QObject):
    """Counts the right presses that reach the application at all, so a
    runner refusing synthetic right-button input skips instead of failing."""

    def __init__(self):
        super().__init__()
        self.presses = 0

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.MouseButtonPress and event.button() == Qt.RightButton
                and obj.isWidgetType()):
            self.presses += 1
        return False


def _state(session, label):
    window, view = session.window, session.view
    app = QApplication.instance()
    print(f'[{label}] active_window_is_editor={window.isActiveWindow()} '
          f'active_window={type(app.activeWindow()).__name__ if app.activeWindow() else None} '
          f'app_state={int(QGuiApplication.applicationState())} '
          f'focus={type(app.focusWidget()).__name__ if app.focusWidget() else None} '
          f'view_has_focus={view.hasFocus()} look={view.mouselook_active} '
          f'grab_is_view={view.mouseGrabber() is view} '
          f'modal={app.activeModalWidget()}', flush=True)


def _right_drag_turns(session, qt_app, label):
    """A real right-drag in the 3D view: does the editor camera turn?"""
    view = session.view
    camera = session.logic.camera.get_editor_camera()
    move_pointer(view, in_view_point(view))
    pump(qt_app, 0.3)
    before = (camera.yaw, camera.pitch)
    post, source, cf = _post_right_drag()
    pump_until(qt_app, lambda: view.mouselook_active, 10.0)
    _state(session, label + ' held')
    for _ in range(15):
        post(kCGEventRightMouseDragged, (20, 6))
        pump(qt_app, 0.05)
    pump(qt_app, 1.0)
    after = (camera.yaw, camera.pitch)
    post(kCGEventRightMouseUp)
    cf.CFRelease(source)
    pump(qt_app, 0.5)
    turned = after != before
    print(f'[{label}] camera {before} -> {after} turned={turned}', flush=True)
    return turned


@pytest.fixture
def glow_editor(fio_session, qt_app, monkeypatch):
    if platform_name() != 'cocoa':
        pytest.skip('the reported bug is macOS input')
    from editor.things import PlayerStart
    from tests.helpers.worlds import box_brush, level_data, make_thing, room
    glow = box_brush('glow', (0, 64, -200), (64, 64, 64), shader='Glow')
    session = fio_session(level_data(brushes=room() + [glow],
                                     things=[make_thing(PlayerStart, 'start', (0, 8, 0))]),
                          size=(800, 450))
    window = session.window
    available = window.screen().availableGeometry()
    frame = window.frameGeometry().size() - window.size()
    window.resize(window.size().boundedTo(available.size() - frame))
    window.move(available.topLeft())
    _activate_macos_app()
    window.raise_()
    window.activateWindow()
    warm_up_input(qt_app, session.view)
    assert pump_until(qt_app, window.isActiveWindow, 10.0), 'the editor is not active'
    import engine.qt_game_view as gv
    logged = []
    real_log = gv.debug_log

    counter = RightPressCounter()
    qt_app.installEventFilter(counter)
    session.right_presses = counter

    def log(category, message):
        if category == 'Input':
            print(f'[debug console] {message}', flush=True)
            logged.append(message)
        return real_log(category, message)
    monkeypatch.setattr(gv, 'debug_log', log)
    session.logged = logged
    session.glow = next(b for b in session.state.brushes if b.get('name') == 'glow')
    yield session
    qt_app.removeEventFilter(counter)


def _pick_glow_colour(session, qt_app):
    """Select the Glow brush and pick a colour with its Glow Color button and
    the real colour dialog, as the user did."""
    window = session.window
    window.set_selected_objects([session.glow])
    pump(qt_app, 0.5)
    button = window.property_editor._widgets.get('glow_color_btn')
    assert button is not None, 'no Glow Color button for the Glow brush'

    def choose():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, QColorDialog):
            dialog.setCurrentColor(QColor(40, 255, 80))
            dialog.accept()
        else:
            QTimer.singleShot(100, choose)
    QTimer.singleShot(300, choose)
    button.click()                     # runs the modal dialog until accepted
    pump(qt_app, 1.0)
    print(f'[picked] colour={session.glow.get("colour")}', flush=True)


def test_right_drag_turns_after_a_glow_colour_change(glow_editor, qt_app):
    session = glow_editor
    if not _right_drag_turns(session, qt_app, 'before'):
        # Nothing has been changed yet, so this is the runner, not the bug:
        # its synthetic right button never became a held look.
        pytest.skip('synthetic right-drag does not drive the look on this runner '
                    f'(right presses reaching Qt: {session.right_presses.presses})')
    _pick_glow_colour(session, qt_app)
    _state(session, 'after picker')
    turned = _right_drag_turns(session, qt_app, 'after')
    assert turned, ('right-drag stopped turning the camera after the Glow colour '
                    'change; the Debug Console said: %s' % session.logged)


def test_control_with_the_old_active_window_check(glow_editor, qt_app, monkeypatch):
    """The same, with the look needing the view's own window active (as before
    the fix). Reported, not asserted: it shows whether that was the cause."""
    import engine.qt_game_view as gv
    monkeypatch.setattr(gv.QtGameView, '_look_window_active',
                        lambda self: self.window().isActiveWindow())
    session = glow_editor
    before = _right_drag_turns(session, qt_app, 'control before')
    _pick_glow_colour(session, qt_app)
    _state(session, 'control after picker')
    after = _right_drag_turns(session, qt_app, 'control after')
    print(f'[control] old check: turned before={before} after={after}', flush=True)
