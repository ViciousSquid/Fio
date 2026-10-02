"""On-screen touch controls: dual virtual sticks + action buttons.

Layout (resolution-independent, expressed as fractions of the surface):

    +---------------------------------------------------+
    |                                          [PAUSE]  |
    |                                                   |
    |                                                   |
    |                                        [ USE ]    |
    |   (left half:                     [JUMP] [FIRE]   |
    |    floating move stick)                           |
    +---------------------------------------------------+

* The **left half** is a *floating* movement stick: wherever the first finger
  lands becomes the stick centre; dragging from there yields move_x/move_y.
* The **right half** (minus the button cluster) is a look pad: dragging turns
  the camera by the frame-to-frame delta.
* Buttons are fixed circular hit regions in screen-fraction space, so they sit
  correctly at any resolution or aspect ratio.

The class is pure math: it takes a snapshot of currently-down pointers
(``{finger_id: (x_px, y_px)}``) and writes into an :class:`InputState`. No
pygame, no GL — unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .state import (
    InputState,
    ACTION_JUMP,
    ACTION_FIRE,
    ACTION_USE,
    ACTION_PAUSE,
)


@dataclass
class TouchButton:
    """A circular button in screen-fraction coordinates."""

    action: str
    cx: float          # centre x as a fraction of width  [0, 1]
    cy: float          # centre y as a fraction of height [0, 1]
    radius: float      # radius as a fraction of the smaller screen dimension

    def contains(self, x: float, y: float, w: float, h: float) -> bool:
        s = min(w, h)
        dx = x - self.cx * w
        dy = y - self.cy * h
        return (dx * dx + dy * dy) <= (self.radius * s) ** 2


@dataclass
class TouchStick:
    """A floating analog stick that appears where a finger first lands."""

    # Region (fractions of the surface) in which a new touch grabs this stick.
    region_x0: float
    region_y0: float
    region_x1: float
    region_y1: float
    radius: float = 0.16   # travel radius as a fraction of the smaller dim
    invert_y: bool = False

    origin: Optional[Tuple[float, float]] = None  # px, set on grab

    def in_region(self, x: float, y: float, w: float, h: float) -> bool:
        return (
            self.region_x0 * w <= x <= self.region_x1 * w
            and self.region_y0 * h <= y <= self.region_y1 * h
        )

    def value(self, x: float, y: float, w: float, h: float) -> Tuple[float, float]:
        if self.origin is None:
            return 0.0, 0.0
        s = min(w, h)
        r = self.radius * s
        vx = (x - self.origin[0]) / r
        # Screen Y grows downward; forward (up on screen) should be +y.
        vy = -(y - self.origin[1]) / r
        if self.invert_y:
            vy = -vy
        return _clamp(vx), _clamp(vy)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


class TouchControls:
    """Multi-touch overlay that maps pointers onto sticks and buttons."""

    def __init__(self, look_sensitivity: float = 0.22, dead_zone: float = 0.08):
        # Movement stick: left ~45% of the width, whole height.
        self.move_stick = TouchStick(0.0, 0.0, 0.45, 1.0)
        self.look_sensitivity = look_sensitivity
        self.dead_zone = dead_zone

        self.buttons = [
            TouchButton(ACTION_FIRE, cx=0.90, cy=0.82, radius=0.085),
            TouchButton(ACTION_JUMP, cx=0.76, cy=0.88, radius=0.075),
            TouchButton(ACTION_USE, cx=0.88, cy=0.62, radius=0.065),
            TouchButton(ACTION_PAUSE, cx=0.95, cy=0.07, radius=0.045),
        ]

        # Persistent per-pointer role assignment across frames.
        # role is one of: "move", "look", "button:<action>"
        self._roles: Dict[int, str] = {}
        self._look_prev: Dict[int, Tuple[float, float]] = {}

    # ------------------------------------------------------------------
    def apply(
        self,
        state: InputState,
        pointers: Dict[int, Tuple[float, float]],
        w: float,
        h: float,
    ) -> None:
        """Fold a snapshot of down pointers into ``state``.

        ``pointers`` maps a stable finger id to its current pixel position.
        Fingers absent from the snapshot are treated as lifted.
        """
        # 1. Retire pointers that lifted since last frame.
        for pid in list(self._roles.keys()):
            if pid not in pointers:
                role = self._roles.pop(pid)
                self._look_prev.pop(pid, None)
                if role == "move":
                    self.move_stick.origin = None

        # 2. Assign roles to newly-arrived pointers by where they landed.
        for pid, (x, y) in pointers.items():
            if pid in self._roles:
                continue
            self._roles[pid] = self._classify(pid, x, y, w, h)

        # 3. Evaluate each active pointer.
        for pid, (x, y) in pointers.items():
            role = self._roles.get(pid)
            if role == "move":
                mx, my = self.move_stick.value(x, y, w, h)
                mx, my = self._apply_dead_zone(mx, my)
                state.add_move(mx, my)
            elif role == "look":
                px, py = self._look_prev.get(pid, (x, y))
                s = min(w, h)
                dx = (x - px) / s * 100.0 * self.look_sensitivity
                dy = (y - py) / s * 100.0 * self.look_sensitivity
                # Screen Y down => dragging up (negative dy) looks up (+).
                state.add_look(dx, -dy)
                self._look_prev[pid] = (x, y)
            elif role and role.startswith("button:"):
                action = role.split(":", 1)[1]
                state.set_button(action, True)

    # ------------------------------------------------------------------
    def _classify(self, pid: int, x: float, y: float, w: float, h: float) -> str:
        for btn in self.buttons:
            if btn.contains(x, y, w, h):
                return f"button:{btn.action}"
        if self.move_stick.in_region(x, y, w, h) and self.move_stick.origin is None:
            self.move_stick.origin = (x, y)
            return "move"
        self._look_prev[pid] = (x, y)
        return "look"

    def _apply_dead_zone(self, x: float, y: float) -> Tuple[float, float]:
        mag = (x * x + y * y) ** 0.5
        if mag <= self.dead_zone or mag == 0.0:
            return 0.0, 0.0
        # Rescale so the dead zone doesn't cause a jump at the threshold.
        scale = (mag - self.dead_zone) / (1.0 - self.dead_zone) / mag
        return _clamp(x * scale), _clamp(y * scale)

    def reset(self) -> None:
        """Drop all pointer assignments (call on focus loss / resume)."""
        self._roles.clear()
        self._look_prev.clear()
        self.move_stick.origin = None
