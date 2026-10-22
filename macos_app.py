"""macOS fit-ups: the menu-bar app name, and Windows layout conventions.

The bold application name in the macOS menu bar is the main bundle's
``CFBundleName``.  A built Fio.app sets that in its Info.plist; ``python
main.py`` runs inside Python's own bundle, so the menu bar says "Python".
The name is read when the application finishes launching, so it is replaced
in the bundle's in-memory info dictionary before the QApplication exists.

Plain ctypes against the Objective-C runtime, so it needs no PyObjC.  It only
ever writes to a dictionary that answers ``setObject:forKey:``: an Objective-C
exception from a failed write would end the process, not raise here.

Qt's macOS style also lays out forms and tab bars the Mac way: form fields stay
at their preferred width and the whole form is centred, labels right-aligned,
tabs centred.  The editor's panels were designed on Windows, where fields
stretch across the panel and everything starts at the left, so on a Mac the
Properties name box and "Targeted by" line came out as a short centred column.
:func:`install_windows_layout_hints` keeps the native Mac look and answers just
those layout questions the way Qt's Windows style does.
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


#: Style hints that decide where forms and tab bars put things.  Everything
#: else -- the look of every control -- stays the Mac style's.
_LAYOUT_HINTS = (
    'SH_FormLayoutFieldGrowthPolicy',
    'SH_FormLayoutFormAlignment',
    'SH_FormLayoutLabelAlignment',
    'SH_FormLayoutWrapPolicy',
    'SH_TabBar_Alignment',
)


def windows_layout_style():
    """The native style, answering form and tab-bar layout hints as Windows.

    None when Qt's Windows style is missing.  Needs a QApplication.
    """
    from PyQt5.QtWidgets import QProxyStyle, QStyle, QStyleFactory

    reference = QStyleFactory.create('Windows')
    if reference is None:
        return None
    hints = {getattr(QStyle, name): reference.styleHint(getattr(QStyle, name))
             for name in _LAYOUT_HINTS}

    class _WindowsLayoutHints(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, returnData=None):
            if hint in hints:
                return hints[hint]
            return super().styleHint(hint, option, widget, returnData)

    # No base style given: the proxy wraps the platform's native style.
    return _WindowsLayoutHints()


def install_windows_layout_hints(app):
    """Lay out forms and tab bars as on Windows; call before any widget exists.

    Returns the installed style, or None when there was none to install.
    """
    style = windows_layout_style()
    if style is not None:
        app.setStyle(style)
    return style
