"""Whole-editor UI scale (Settings > Display > UI Scale).

The editor is laid out in fixed logical pixels -- toolbar buttons, the Play
button, dock minimums, paddings -- tuned on a 1920x1080 Windows desktop, where
Qt 5 maps a logical pixel to a physical one.  macOS lays every window out in
points instead, and a laptop's desktop is small in points (a MacBook Air's is
1440x900 or 1470x956), so the same layout comes out about a third larger
relative to the screen there.  Text does not have that problem: macOS sizes
fonts at 72 points per inch, so 11pt text takes the same share of a Mac screen
as it does of a 1920-wide Windows one.

So the scale applies to controls and not to text.  It is Qt's per-screen
factor, ``QT_SCREEN_SCALE_FACTORS``, which scales widgets, icons, pixel-sized
fonts and the 3D view's framebuffer, keeps Retina rendering sharp, and -- as
Qt documents -- leaves point-sized fonts alone: it lowers the screen's logical
DPI by the same factor instead.  At 75% on a Mac that DPI is 96, the same as
Windows, so the editor's pixel and point sizes relate exactly as they do
there.  Qt reads the factor once, when the QApplication is created, so it is
applied from main.py before that and a change needs a restart.

``[Display] ui_scale`` is ``auto`` or a whole percentage.  ``auto`` is 100%
everywhere except macOS, where it shrinks the reference desktop onto the main
screen (the first in Qt's list, which is the main one).  A
``QT_SCREEN_SCALE_FACTORS`` or ``QT_SCALE_FACTOR`` already in the environment
always wins, so those variables still work as overrides.
"""

import os
import sys

#: Percentages offered in Settings, besides Auto.
UI_SCALE_CHOICES = (65, 70, 75, 80, 85, 90, 100, 110, 125, 150)

#: Bounds for a hand-edited settings.ini value.
MIN_UI_SCALE = 50
MAX_UI_SCALE = 200

#: The desktop the editor's pixel sizes were tuned on; auto fits it.
REFERENCE_DESKTOP = (1920, 1080)

#: Points at the top and bottom of a Mac screen that a window cannot use: the
#: menu bar and a Dock at its default size.  CoreGraphics reports the whole
#: display, so the reference is fitted to what is left of it.
_MAC_RESERVED_HEIGHT = 100

#: Auto never shrinks controls below this.
_AUTO_FLOOR = 65

def parse_ui_scale(value):
    """``None`` for auto, else the percentage clamped to the allowed range."""
    text = str(value).strip().lower().rstrip('%')
    if text in ('', 'auto'):
        return None
    try:
        percent = int(round(float(text)))
    except ValueError:
        return None
    return max(MIN_UI_SCALE, min(MAX_UI_SCALE, percent))


def auto_ui_scale(screen_size):
    """Percent that fits REFERENCE_DESKTOP into *screen_size* (usable points).

    Rounded down to a 5% step, never above 100% and never below the floor.
    ``None`` (screen unknown) is 100%.
    """
    if not screen_size:
        return 100
    width, height = screen_size
    if width <= 0 or height <= 0:
        return 100
    fit = min(1.0, width / REFERENCE_DESKTOP[0],
              height / (REFERENCE_DESKTOP[1] - _MAC_RESERVED_HEIGHT))
    percent = int(fit * 100) // 5 * 5
    return max(_AUTO_FLOOR, min(100, percent))


def _mac_main_screen_points():
    """Usable ``(width, height)`` of the main display in points, or None.

    Asked of CoreGraphics directly because Qt cannot be asked before the
    QApplication exists, which is too late to set the scale.
    """
    try:
        import ctypes

        class _CGPoint(ctypes.Structure):
            _fields_ = [('x', ctypes.c_double), ('y', ctypes.c_double)]

        class _CGSize(ctypes.Structure):
            _fields_ = [('width', ctypes.c_double), ('height', ctypes.c_double)]

        class _CGRect(ctypes.Structure):
            _fields_ = [('origin', _CGPoint), ('size', _CGSize)]

        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGDisplayBounds.restype = _CGRect
        bounds = cg.CGDisplayBounds(cg.CGMainDisplayID())
        width = int(bounds.size.width)
        height = int(bounds.size.height) - _MAC_RESERVED_HEIGHT
        if width <= 0 or height <= 0:
            return None
        return width, height
    except Exception:
        return None


def resolve_ui_scale(setting, platform=None, screen_size=None):
    """The scale factor (1.0 = 100%) for the ``ui_scale`` *setting*.

    *platform* defaults to ``sys.platform``; *screen_size* is only consulted
    for auto on macOS, and is read from the main display when not given.
    """
    percent = parse_ui_scale(setting)
    if percent is None:
        if (platform or sys.platform) != 'darwin':
            return 1.0
        if screen_size is None:
            screen_size = _mac_main_screen_points()
        percent = auto_ui_scale(screen_size)
    return percent / 100.0


def apply_ui_scale(config):
    """Set ``QT_SCREEN_SCALE_FACTORS`` from *config*; call before the QApplication.

    Returns the factor in effect.  Nothing is set at 100%, or when either Qt
    scaling variable is already in the environment.
    """
    if os.environ.get('QT_SCREEN_SCALE_FACTORS') or os.environ.get('QT_SCALE_FACTOR'):
        return 1.0
    factor = resolve_ui_scale(config.get('Display', 'ui_scale', fallback='auto'))
    if abs(factor - 1.0) > 1e-6:
        os.environ['QT_SCREEN_SCALE_FACTORS'] = f'{factor:g}'
    return factor
