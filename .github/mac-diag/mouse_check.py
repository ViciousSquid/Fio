"""Temporary: macOS relative mouse look, end to end on a real Mac runner."""
import ctypes, os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from PyQt5.QtWidgets import QApplication, QWidget
from PyQt5.QtCore import QTimer, QPoint
from PyQt5.QtGui import QCursor
import macos_app
import engine.qt_game_view as q

app = QApplication(sys.argv)
cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
class CGPoint(ctypes.Structure):
    _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]
cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
cg.CFRelease = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation').CFRelease
cg.CFRelease.argtypes = [ctypes.c_void_p]

def post_move(dx, dy):
    p = QCursor.pos()
    e = cg.CGEventCreateMouseEvent(None, 5, CGPoint(p.x(), p.y()), 0)   # kCGEventMouseMoved
    cg.CGEventSetIntegerValueField(e, 4, dx)                            # kCGMouseEventDeltaX
    cg.CGEventSetIntegerValueField(e, 5, dy)                            # kCGMouseEventDeltaY
    cg.CGEventPost(0, e)                                                # kCGHIDEventTap
    cg.CFRelease(e)

class GS:
    total = [0.0, 0.0]
    def set_mouse_delta(self, dx, dy):
        self.total[0] += dx; self.total[1] += dy

class View(QWidget):
    _recentre_look_cursor = q.QtGameView._recentre_look_cursor
    _mouse_look_captured = q.QtGameView._mouse_look_captured
    _poll_mouse_look = q.QtGameView._poll_mouse_look
    _poll_mouse_look_macos = q.QtGameView._poll_mouse_look_macos
    _attach_mac_cursor = q.QtGameView._attach_mac_cursor
    mouselook_active = False; play_mode = True; pause_menu = None
    console_overlay_active = False; _actor_pick = None

v = View(); v.resize(600, 400); v.show(); v.raise_(); v.activateWindow()
v.game_state = GS()
v._mac_mouse = macos_app.mouse_movement_filter()
v._mac_cursor_detached = False
v._skip_next_look_poll = True
app.installNativeEventFilter(v._mac_mouse)
print('filter', v._mac_mouse is not None, flush=True)

frames = {'n': 0}
posted = [0, 0]
def frame():
    frames['n'] += 1
    n = frames['n']
    if n == 2:
        print('active window:', v.isActiveWindow(), flush=True)
    v._poll_mouse_look()
    if n == 4:
        print('detached:', v._mac_cursor_detached, 'cursor at', v.mapFromGlobal(QCursor.pos()),
              'centre', v.rect().center(), flush=True)
        frames['start'] = QCursor.pos()
    if 5 <= n < 65:                                   # 60 frames x 4 moves
        for _ in range(4):
            post_move(7, -3); posted[0] += 7; posted[1] -= 3
    if n == 70:
        print('posted', posted, '-> turned', GS.total, flush=True)
        print('cursor moved while detached:', QCursor.pos() - frames['start'], flush=True)
        v.pause_menu = object()                       # pause: must release
    if n == 73:
        print('paused -> detached:', v._mac_cursor_detached, flush=True)
        before = list(GS.total); post_move(50, 50)
        frames['before'] = before
    if n == 76:
        print('movement while paused turned:', [GS.total[0] - frames['before'][0], GS.total[1] - frames['before'][1]], flush=True)
        app.quit()
t = QTimer(); t.timeout.connect(frame); t.start(16)
if not v.isActiveWindow():
    QTimer.singleShot(500, lambda: (v.raise_(), v.activateWindow()))
app.exec_()
macos_app.set_cursor_follows_mouse(True)
