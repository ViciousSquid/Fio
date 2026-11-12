"""Play: a real left click fires, even with the hidden pointer over the Play button.

The bug: Play hid the cursor and turned the camera from mouse movement but
never grabbed the mouse, so a click went to whatever widget the real pointer
was over. With the pointer resting on the Play button (macOS pins it where
it was when Play began; elsewhere a flick between two re-centres can carry it
there), clicking to fire pressed Stop.

Unlike the offscreen tests (tests/editor/test_play_mouse_grab.py), this one
runs on a real window system and clicks through the operating system's own
input -- mouse_event on Windows, XTest (xdotool) on X11, CGEventPost on
macOS, a virtual pointer device on Wayland (wayland_virtual_pointer.py) -- so
the window system, not Qt's test helpers, decides which widget the click
lands on. It needs a desktop session and moves the real pointer, so it runs
only when ``FIO_NATIVE_INPUT=1`` (.github/workflows/play-click-native.yml).
"""

import ctypes
import os
import shutil
import subprocess
import sys
import time

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtCore import QEvent, QObject, QPoint, Qt
from PyQt5.QtGui import QCursor, QGuiApplication

pytestmark = [
    pytest.mark.qt,
    pytest.mark.gl,
    pytest.mark.skipif(os.environ.get('FIO_NATIVE_INPUT') != '1',
                       reason='moves the real pointer; set FIO_NATIVE_INPUT=1'),
]


# -- the window system's own pointer and click -------------------------------
#
# Each platform's real input path. A click lands wherever the pointer is.

def platform_name():
    return QGuiApplication.platformName()


def _swaymsg(*command):
    subprocess.run([shutil.which('swaymsg'), *command], check=True, timeout=10,
                   stdout=subprocess.DEVNULL)


class WaylandPointer:
    """The long-lived virtual pointer process (wayland_virtual_pointer.py)."""

    def __init__(self):
        script = os.path.join(os.path.dirname(__file__), 'wayland_virtual_pointer.py')
        self.proc = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        line = self.proc.stdout.readline().strip()
        assert line == 'ready', f'no virtual pointer: {line!r}'

    def send(self, command):
        self.proc.stdin.write(command + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline().strip()
        assert line == 'ok', f'virtual pointer {command!r}: {line!r}'

    def close(self):
        try:
            self.proc.stdin.write('quit\n')
            self.proc.stdin.flush()
            self.proc.wait(5)
        except Exception:
            self.proc.kill()


_wayland = {}       # 'pointer': the WaylandPointer while a Wayland test runs


def _cg():
    cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')

    class CGPoint(ctypes.Structure):
        _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]

    cg.CGPoint = CGPoint
    cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           CGPoint, ctypes.c_uint32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    return cg, cf


def move_pointer(widget, local):
    """Put the real pointer over *local* (a point in *widget*), synchronously."""
    name = platform_name()
    if name == 'wayland':
        # A Wayland client does not know where its window is, so Qt's global
        # coordinates mean nothing; the editor is full screen at the layout's
        # origin (see `armed`), so its window coordinates are the layout's.
        window = widget.window()
        at = widget.mapTo(window, local)
        size = window.screen().geometry().size()
        _wayland['pointer'].send(f'move {at.x()} {at.y()} {size.width()} {size.height()}')
        return
    point = widget.mapToGlobal(local)
    if name == 'cocoa':
        cg, _ = _cg()
        cg.CGWarpMouseCursorPosition(cg.CGPoint(point.x(), point.y()))
    elif name == 'xcb':
        ratio = QGuiApplication.primaryScreen().devicePixelRatio()
        # (No --sync: it waits for movement, forever if already there.)
        subprocess.run([shutil.which('xdotool'), 'mousemove',
                        str(round(point.x() * ratio)), str(round(point.y() * ratio))],
                       check=True, timeout=10)
    else:                                   # windows: SetCursorPos
        QCursor.setPos(point)


def os_click():
    """A left click through the window system's input queue."""
    name = platform_name()
    if name == 'wayland':
        _wayland['pointer'].send('click')
    elif name == 'cocoa':
        cg, cf = _cg()
        probe = cg.CGEventCreate(None)
        where = cg.CGEventGetLocation(probe)
        cf.CFRelease(probe)
        kCGEventLeftMouseDown, kCGEventLeftMouseUp, kCGHIDEventTap = 1, 2, 0
        for kind in (kCGEventLeftMouseDown, kCGEventLeftMouseUp):
            event = cg.CGEventCreateMouseEvent(None, kind, where, 0)
            cg.CGEventPost(kCGHIDEventTap, event)
            cf.CFRelease(event)
            time.sleep(0.03)
    elif name == 'xcb':
        subprocess.run([shutil.which('xdotool'), 'click', '--delay', '30', '1'],
                       check=True, timeout=10)
    else:                                   # windows: the input queue
        user32 = ctypes.windll.user32
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


class PressCounter(QObject):
    """Counts the left presses that reach the application at all, so a
    platform refusing synthetic input fails as that, not as a wrong verdict."""

    def __init__(self):
        super().__init__()
        self.presses = 0        # a count: holding the widgets would leak them
        self.where = None       # global position of the last one

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton
                and obj.isWidgetType()):
            self.presses += 1
            self.where = QPoint(event.globalPos())
        return False


# -- helpers ----------------------------------------------------------------

def pump(app, seconds):
    """Run the real event loop (the view's 16 ms frame timer included)."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)


def pump_until(app, predicate, timeout):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def shots_queued(game_state):
    with game_state._shot_lock:
        return len(game_state._shot_queue)


def clear_shots(game_state):
    with game_state._shot_lock:
        game_state._shot_queue.clear()


def report(label, session):
    view, button = session.view, session.window.play_button
    print(f'[{label}] platform={QGuiApplication.platformName()} '
          f'play_mode={view.play_mode} active={session.window.isActiveWindow()} '
          f'grabber_is_view={view.mouseGrabber() is view} '
          f'cursor={QCursor.pos()} '
          f'view_centre={view.mapToGlobal(view.rect().center())} '
          f'play_button={button.mapToGlobal(button.rect().topLeft())}+'
          f'{button.width()}x{button.height()} '
          f'shots={shots_queued(session.game_state)}', flush=True)


@pytest.fixture
def armed(fio_session, qt_app):
    """_SHOWCASE in Play, the window active, the shotgun equipped and ready."""
    session = fio_session('maps/_SHOWCASE.json', size=(800, 450))
    window = session.window
    if platform_name() == 'wayland':
        _wayland['pointer'] = WaylandPointer()
        # Wayland clients do not know where their window is; full screen, the
        # window's origin is the layout's, so Qt's coordinates are sway's.
        _swaymsg('fullscreen', 'enable')
        screen = window.screen().geometry()
        pump_until(qt_app, lambda: window.size() == screen.size(), 5.0)
        assert window.size() == screen.size(), \
            f'the editor did not go full screen: {window.size()} on {screen}'
    window.raise_()
    window.activateWindow()
    if not pump_until(qt_app, window.isActiveWindow, 5.0):
        # Windows refuses activation to a process that has not had input:
        # a real click on the status bar (inert) gives it some.
        bar = window.statusBar()
        move_pointer(bar, bar.rect().center())
        os_click()
        pump_until(qt_app, window.isActiveWindow, 5.0)
    assert window.isActiveWindow(), 'the editor window could not be made active'

    session.start_play()
    # _SHOWCASE opens with a cutscene, which holds all player input: play it
    # out (about 20 s of game time) as a player would sit through it.
    cutscenes = session.logic.cutscene_runtime
    for _ in range(60 * 120):
        if not cutscenes.state:
            break
        session.step(1)
    assert not cutscenes.state, 'the opening cutscene did not finish'
    combat = session.logic.combat_runtime
    combat.active_weapon = 'gun2'      # the shotgun (engine/items.py)
    combat.player_ammo = 20
    session.step(1)                     # publish active_weapon / shot_ready
    published = session.game_state.published
    assert published('active_weapon') == 'gun2' and published('shot_ready'), \
        'the shotgun was not published as ready'
    session.view.setFocus()
    # Real frames: the mouse-look poll centres the pointer and takes hold.
    pump(qt_app, 0.5)
    report('armed', session)
    assert session.view.play_mode
    session.presses = PressCounter()
    qt_app.installEventFilter(session.presses)
    yield session
    qt_app.removeEventFilter(session.presses)
    pointer = _wayland.pop('pointer', None)
    if pointer is not None:
        pointer.close()


def assert_click_fired_the_shotgun(session, qt_app, label):
    """After a real click: it reached the app, Play goes on, and the shotgun
    fires on the next logic tick -- its ammunition goes down."""
    pump(qt_app, 0.3)
    report(label, session)
    print(f'[{label}] presses={session.presses.presses} at={session.presses.where}', flush=True)
    assert session.presses.presses, (
        'no left press reached the application: the window system did not '
        'deliver the synthetic click (input permission?)')
    assert session.view.play_mode, 'the click left Play'
    assert shots_queued(session.game_state) >= 1, 'the click did not fire'
    combat = session.logic.combat_runtime
    ammo = combat.player_ammo
    session.step(1)
    print(f'[{label}] shotgun ammo {ammo} -> {combat.player_ammo}', flush=True)
    assert combat.player_ammo == ammo - 1, 'the shotgun did not fire'


def test_left_click_fires_the_equipped_weapon(armed, qt_app):
    session = armed
    view = session.view
    move_pointer(view, view.rect().center())
    clear_shots(session.game_state)
    os_click()
    assert_click_fired_the_shotgun(session, qt_app, 'click in the view')


def test_left_click_fires_even_with_the_hidden_pointer_over_the_play_button(armed, qt_app):
    """The reported bug: the pointer is on Play/Stop when the player clicks."""
    session = armed
    button = session.window.play_button
    assert button.isVisible() and button.text() == 'Stop'
    window = session.window
    if platform_name() == 'wayland':      # full screen: the window is the screen
        on_screen = window.rect().contains(button.mapTo(window, button.rect().center()))
    else:
        target = button.mapToGlobal(button.rect().center())
        screen = QGuiApplication.screenAt(target)
        on_screen = screen is not None and screen.geometry().contains(target)
    assert on_screen, 'the Play button is off screen'
    clear_shots(session.game_state)
    stops = []
    button.clicked.connect(lambda *_: stops.append(True))
    # Move the real pointer and click at once -- no event processing, so no
    # frame re-centres it in between: the OS queues the press over the button.
    move_pointer(button, button.rect().center())
    os_click()
    pump(qt_app, 0.3)
    assert not stops, 'the click pressed the Play/Stop button'
    assert_click_fired_the_shotgun(session, qt_app, 'click over the Play button')


def test_control_without_plays_mouse_grab_the_click_presses_stop(armed, qt_app, monkeypatch):
    """The control: the same click with Play's mouse grab switched off must
    reproduce the reported bug here, or the test above proves nothing on this
    platform."""
    session = armed
    view, button, window = session.view, session.window.play_button, session.window
    monkeypatch.setattr(view, '_sync_play_mouse_grab', lambda held: None)
    if view.mouseGrabber() is view:
        view.releaseMouse()
    pump(qt_app, 0.1)
    clear_shots(session.game_state)
    stops = []
    button.clicked.connect(lambda *_: stops.append(True))
    move_pointer(button, button.rect().center())
    os_click()
    pump(qt_app, 0.3)
    report('control: click over the Play button, no grab', session)
    assert session.presses.presses, 'no left press reached the application'
    assert stops and not view.play_mode, (
        'without the grab the click should have pressed Stop (the reported '
        'bug); it did not, so this platform does not exercise the bug')
    assert shots_queued(session.game_state) == 0
