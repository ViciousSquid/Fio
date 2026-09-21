"""Platform host interface shared by the desktop and Android backends."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..input.state import InputState


@dataclass
class HostConfig:
    """Window / surface configuration for a host."""

    title: str = "Fio Player"
    width: int = 1280            # desktop window size; ignored (native) on Android
    height: int = 720
    target_fps: int = 60
    gles_major: int = 3          # request an OpenGL ES 3.x context
    gles_minor: int = 0
    depth_bits: int = 24
    stencil_bits: int = 8        # portals need a stencil buffer
    fullscreen: bool = False
    vsync: bool = True
    show_touch_controls: bool = True   # draw the on-screen virtual controls


@dataclass
class FrameCallbacks:
    """Hooks the app registers with the host.

    The host guarantees ordering: ``on_gl_ready`` (once, and again after a
    context loss) before any ``on_frame``; ``on_pause``/``on_resume`` around
    background transitions; ``on_shutdown`` exactly once at the end.
    """

    on_gl_ready: Callable[["PlatformHost"], None] = lambda host: None
    on_frame: Callable[[float, InputState], None] = lambda dt, inp: None
    on_resize: Callable[[int, int], None] = lambda w, h: None
    on_pause: Callable[[], None] = lambda: None
    on_resume: Callable[[], None] = lambda: None
    on_shutdown: Callable[[], None] = lambda: None


class PlatformHost(abc.ABC):
    """Owns the surface, the event pump, and the frame loop."""

    def __init__(self, config: HostConfig, callbacks: FrameCallbacks):
        self.config = config
        self.callbacks = callbacks
        self.input_state = InputState()
        self.width = config.width
        self.height = config.height

    @abc.abstractmethod
    def run(self) -> None:
        """Create the surface and block, running the frame loop until quit."""

    @abc.abstractmethod
    def request_quit(self) -> None:
        """Ask the loop to exit after the current frame."""

    def swap_buffers(self) -> None:  # pragma: no cover - backend specific
        """Present the rendered frame. Overridden by concrete hosts."""
        raise NotImplementedError
