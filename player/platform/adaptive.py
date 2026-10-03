"""Adaptive render resolution.

Mobile GPUs vary wildly. Rather than render at the full native panel resolution
(often 1080p+ on a phone, which many integrated GPUs cannot sustain for a lit,
shadowed scene), the player renders the 3D scene into an offscreen framebuffer
at a *scaled* resolution and blits it to the panel. This controller watches the
frame time and nudges the scale up or down to hold a target frame rate — the
same idea as console dynamic-resolution scaling.

Pure logic: feed it frame times, read back a scale factor. The renderer decides
how to use the scale (FBO size); this class never touches GL.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdaptiveResolution:
    """EWMA-driven dynamic resolution controller.

    Parameters
    ----------
    target_fps:
        Frame rate to hold. Scale drops when we run slower, rises when we have
        headroom.
    min_scale / max_scale:
        Clamp for the linear resolution scale (0.5 => render at half width and
        half height => a quarter of the pixels).
    step:
        Maximum change per adjustment, to avoid visible resolution pops.
    """

    target_fps: float = 60.0
    min_scale: float = 0.5
    max_scale: float = 1.0
    step: float = 0.05
    smoothing: float = 0.1     # EWMA weight for new frame-time samples

    scale: float = 1.0
    _avg_frame_ms: float = 0.0  # seeded from target_fps in __post_init__
    _cooldown: int = 0

    def __post_init__(self) -> None:
        self._avg_frame_ms = 1000.0 / max(self.target_fps, 1.0)
        self.scale = max(self.min_scale, min(self.max_scale, self.scale))

    def submit_frame(self, frame_ms: float) -> float:
        """Report the last frame's duration (ms); return the new scale.

        The scale changes at most ``step`` per call and only after a short
        cooldown, so it settles instead of oscillating.
        """
        if frame_ms <= 0.0:
            return self.scale
        # Exponentially-weighted moving average of frame time.
        a = self.smoothing
        self._avg_frame_ms = (1.0 - a) * self._avg_frame_ms + a * frame_ms

        if self._cooldown > 0:
            self._cooldown -= 1
            return self.scale

        target_ms = 1000.0 / self.target_fps
        # Hysteresis band so we don't twitch around the target.
        if self._avg_frame_ms > target_ms * 1.10:
            self.scale = max(self.min_scale, self.scale - self.step)
            self._cooldown = 8
        elif self._avg_frame_ms < target_ms * 0.80 and self.scale < self.max_scale:
            self.scale = min(self.max_scale, self.scale + self.step)
            self._cooldown = 20  # rise slowly, drop quickly
        return self.scale

    def render_size(self, panel_w: int, panel_h: int) -> "tuple[int, int]":
        """The offscreen render dimensions for the current scale (even numbers)."""
        rw = max(2, int(round(panel_w * self.scale)) & ~1)
        rh = max(2, int(round(panel_h * self.scale)) & ~1)
        return rw, rh

    @property
    def average_fps(self) -> float:
        return 1000.0 / self._avg_frame_ms if self._avg_frame_ms > 0 else 0.0
