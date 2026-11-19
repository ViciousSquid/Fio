"""Fio's OpenGL surface format, in one place.

The 3D view asks for it (``QtGameView.setFormat``), and ``main.py`` makes it
the application default before the ``QApplication`` exists. The default
matters as much as the view's own request: a ``QOpenGLWidget`` shares its
context with a context its top-level window creates from the *window's*
format, which is the default. Where the two cannot share -- macOS will not
share a core-profile context with a legacy one ("Could not create
NSOpenGLContext with shared context, falling back to unshared context") --
Qt rebuilds the widget's context, and Fio's renderer with it, whenever the
widget is shown again after its window was hidden.

Anything that builds Fio's editor windows outside ``main.py`` (the test
harness, tools/bench_kiosk_presentation.py) calls
:func:`set_default_surface_format` first, so it runs the same GL setup.
"""

from __future__ import annotations


def fio_surface_format(swap_interval=None):
    """OpenGL 3.3 core, 24-bit depth, 8-bit stencil; *swap_interval* (0 or
    1) if given, else Qt's default."""
    from PyQt5.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    if swap_interval is not None:
        fmt.setSwapInterval(int(swap_interval))
    return fmt


def set_default_surface_format(swap_interval=None):
    """Make :func:`fio_surface_format` the application default. Call before
    the QApplication is created (later, windows already made keep theirs).
    Returns the format."""
    from PyQt5.QtGui import QSurfaceFormat
    fmt = fio_surface_format(swap_interval)
    QSurfaceFormat.setDefaultFormat(fmt)
    return fmt
