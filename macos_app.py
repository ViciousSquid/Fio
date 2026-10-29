"""macOS fit-ups: the menu-bar app name, and relative mouse look.

The bold application name in the macOS menu bar is the main bundle's
``CFBundleName``.  A built Fio.app sets that in its Info.plist; ``python
main.py`` runs inside Python's own bundle, so the menu bar says "Python".
The name is read when the application finishes launching, so it is replaced
in the bundle's in-memory info dictionary before the QApplication exists.

Plain ctypes against the Objective-C runtime, so it needs no PyObjC.  It only
ever writes to a dictionary that answers ``setObject:forKey:``: an Objective-C
exception from a failed write would end the process, not raise here.

Forms, tab bars and every other control are laid out as on Windows on every
platform by :mod:`editor.desktop_style`.
"""

import ctypes
import ctypes.util


def _objc():
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
    # NSBundle and NSString live in Foundation; loading it registers them.
    ctypes.cdll.LoadLibrary(ctypes.util.find_library('Foundation'))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(objc, restype, receiver, selector, *args):
    """``[receiver selector ...]``; *args* are ``(ctype, value)`` pairs.

    objc_msgSend is given exact argument types per call: on arm64 calling it
    as a variadic function passes the arguments in the wrong registers.
    """
    send = ctypes.CFUNCTYPE(
        restype, ctypes.c_void_p, ctypes.c_void_p, *(t for t, _ in args)
    )(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value)
    return send(receiver, objc.sel_registerName(selector), *(v for _, v in args))


def set_process_app_name(name):
    """Show *name* in the menu bar; call before the QApplication exists.

    Returns True when the name was set.  Never raises.
    """
    try:
        objc = _objc()
        ptr = ctypes.c_void_p
        bundle = _send(objc, ptr, objc.objc_getClass(b'NSBundle'), b'mainBundle')
        if not bundle:
            return False
        value = _send(objc, ptr, objc.objc_getClass(b'NSString'),
                      b'stringWithUTF8String:', (ctypes.c_char_p, name.encode()))
        key = _send(objc, ptr, objc.objc_getClass(b'NSString'),
                    b'stringWithUTF8String:', (ctypes.c_char_p, b'CFBundleName'))
        done = False
        for getter in (b'localizedInfoDictionary', b'infoDictionary'):
            info = _send(objc, ptr, bundle, getter)
            if not info:
                continue
            mutable = _send(objc, ctypes.c_bool, info, b'respondsToSelector:',
                            (ptr, objc.sel_registerName(b'setObject:forKey:')))
            if mutable:
                _send(objc, None, info, b'setObject:forKey:', (ptr, value), (ptr, key))
                done = True
        return done
    except Exception:
        return False


def process_app_name():
    """The main bundle's current ``CFBundleName``, or None (for checks)."""
    try:
        objc = _objc()
        ptr = ctypes.c_void_p
        bundle = _send(objc, ptr, objc.objc_getClass(b'NSBundle'), b'mainBundle')
        key = _send(objc, ptr, objc.objc_getClass(b'NSString'),
                    b'stringWithUTF8String:', (ctypes.c_char_p, b'CFBundleName'))
        value = _send(objc, ptr, bundle, b'objectForInfoDictionaryKey:', (ptr, key))
        if not value:
            return None
        text = _send(objc, ctypes.c_char_p, value, b'UTF8String')
        return text.decode() if text else None
    except Exception:
        return None


# -- Relative mouse (mouse look) -----------------------------------------------
#
# Mouse look used to re-centre the cursor with QCursor.setPos() and measure how
# far it had moved. On macOS that re-centre is asynchronous -- Qt posts it as an
# input event -- so the position read back could predate it, counting movement
# twice or jumping. Games on macOS instead detach the cursor from the mouse
# (CGAssociateMouseAndMouseCursorPosition) so it stays put, and read each
# event's own movement (NSEvent deltaX/deltaY). Qt hands every NSEvent to
# native event filters, which is where the movement is collected.

#: NSEventType values that carry pointer movement: moved, left-, right- and
#: other-mouse dragged.
_MOVEMENT_EVENTS = frozenset((5, 6, 7, 27))


def set_cursor_follows_mouse(follows):
    """Attach (True) or detach (False) the cursor from mouse movement.

    Detached, the cursor stays where it is while the mouse still produces
    movement events. Returns False if CoreGraphics could not be called.
    """
    try:
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGAssociateMouseAndMouseCursorPosition.argtypes = [ctypes.c_bool]
        cg.CGAssociateMouseAndMouseCursorPosition.restype = ctypes.c_int32
        return cg.CGAssociateMouseAndMouseCursorPosition(bool(follows)) == 0
    except Exception:
        return False


def mouse_movement_filter():
    """A Qt native event filter summing mouse movement from every NSEvent.

    ``take()`` returns the (dx, dy) in points since the last call and resets
    it. Install with QCoreApplication.installNativeEventFilter and keep a
    reference; returns None when the Objective-C runtime is unavailable.
    """
    from PyQt5.QtCore import QAbstractNativeEventFilter

    try:
        objc = _objc()
        type_sel = objc.sel_registerName(b'type')
        dx_sel = objc.sel_registerName(b'deltaX')
        dy_sel = objc.sel_registerName(b'deltaY')
        address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        event_type = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p)(address)
        event_delta = ctypes.CFUNCTYPE(ctypes.c_double, ctypes.c_void_p, ctypes.c_void_p)(address)
    except Exception:
        return None

    class _MouseMovement(QAbstractNativeEventFilter):
        def __init__(self):
            super().__init__()
            self.dx = 0.0
            self.dy = 0.0

        def nativeEventFilter(self, kind, message):
            if bytes(kind) == b'mac_generic_NSEvent' and message:
                event = int(message)
                if event_type(event, type_sel) in _MOVEMENT_EVENTS:
                    self.dx += event_delta(event, dx_sel)
                    self.dy += event_delta(event, dy_sel)
            return False, 0

        def take(self):
            dx, dy = self.dx, self.dy
            self.dx = self.dy = 0.0
            return dx, dy

    return _MouseMovement()
