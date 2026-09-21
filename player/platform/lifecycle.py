"""App lifecycle state machine (suspend / resume).

Android can pause the app at any moment (home button, incoming call, screen
off). When that happens the EGL surface may be destroyed and all GL objects
(textures, VBOs, shaders) lost, so the player must:

* stop the simulation and audio on pause,
* on resume, re-create GL resources if the context was lost, then continue.

This class is the pure state machine that decides *what* transition happened;
the host feeds it OS events and the app reacts to the emitted phase. Keeping it
separate makes the (fiddly, race-prone) transition logic testable.
"""

from __future__ import annotations

from enum import Enum, auto


class LifecyclePhase(Enum):
    CREATED = auto()      # constructed, no surface yet
    RUNNING = auto()      # foreground, surface valid, simulating
    PAUSED = auto()       # background: sim + audio halted, may lose surface
    SURFACE_LOST = auto() # backgrounded and the GL context/surface is gone
    STOPPED = auto()      # shutting down


class LifecycleState:
    """Tracks foreground/background and GL-surface validity."""

    def __init__(self) -> None:
        self.phase = LifecyclePhase.CREATED
        self.surface_valid = False
        # Set true when a resume needs GL resources rebuilt (context was lost).
        self.needs_gl_reload = False

    # -- transitions ----------------------------------------------------
    def on_surface_created(self) -> None:
        """A GL-ES surface became available (initial start or resume)."""
        was_lost = self.phase == LifecyclePhase.SURFACE_LOST
        self.surface_valid = True
        if was_lost:
            self.needs_gl_reload = True
        if self.phase in (LifecyclePhase.CREATED, LifecyclePhase.SURFACE_LOST,
                           LifecyclePhase.PAUSED):
            self.phase = LifecyclePhase.RUNNING

    def on_surface_destroyed(self) -> None:
        """The GL-ES surface was torn down (backgrounded on many devices)."""
        self.surface_valid = False
        # Only downgrade to SURFACE_LOST from a background state.
        if self.phase in (LifecyclePhase.PAUSED, LifecyclePhase.RUNNING):
            self.phase = LifecyclePhase.SURFACE_LOST

    def on_pause(self) -> None:
        """App moved to the background."""
        if self.phase == LifecyclePhase.RUNNING:
            self.phase = LifecyclePhase.PAUSED

    def on_resume(self) -> None:
        """App returned to the foreground.

        If the surface survived, resume immediately; otherwise we wait for a
        subsequent :meth:`on_surface_created`.
        """
        if self.phase == LifecyclePhase.PAUSED and self.surface_valid:
            self.phase = LifecyclePhase.RUNNING

    def on_stop(self) -> None:
        self.phase = LifecyclePhase.STOPPED
        self.surface_valid = False

    # -- queries --------------------------------------------------------
    @property
    def should_render(self) -> bool:
        return self.phase == LifecyclePhase.RUNNING and self.surface_valid

    @property
    def should_simulate(self) -> bool:
        return self.phase == LifecyclePhase.RUNNING

    @property
    def is_alive(self) -> bool:
        return self.phase != LifecyclePhase.STOPPED

    def consume_gl_reload(self) -> bool:
        """Return True once if GL resources must be rebuilt, then clear it."""
        if self.needs_gl_reload:
            self.needs_gl_reload = False
            return True
        return False
