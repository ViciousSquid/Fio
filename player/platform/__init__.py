"""Platform host abstraction for the Fio Player.

The host owns the OS-facing concerns the desktop editor previously handled
through PyQt5: creating the window/GL-ES surface, pumping OS events into the
normalized :class:`~player.input.state.InputState`, running the frame loop, and
routing app-lifecycle transitions (pause/resume) to the game.

Two concrete hosts are provided:

* :class:`~player.platform.desktop.DesktopHost` — a pygame/SDL2 window used as
  the on-desktop development harness. It requests an OpenGL ES-compatible
  context so shaders and GL usage match the device.
* :class:`~player.platform.android.AndroidHost` — the same pygame/SDL2 host with
  Android-specific lifecycle wiring (``APP_WILLENTERBACKGROUND`` /
  ``APP_DIDENTERFOREGROUND``) and full-screen immersive surface handling.

:class:`~player.platform.lifecycle.LifecycleState` and
:class:`~player.platform.adaptive.AdaptiveResolution` are pure logic and are
unit-testable without a display.
"""

from .base import PlatformHost, HostConfig, FrameCallbacks
from .lifecycle import LifecycleState, LifecyclePhase
from .adaptive import AdaptiveResolution

__all__ = [
    "PlatformHost",
    "HostConfig",
    "FrameCallbacks",
    "LifecycleState",
    "LifecyclePhase",
    "AdaptiveResolution",
]
