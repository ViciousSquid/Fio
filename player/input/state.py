"""Normalized, source-agnostic input state.

Every frame the platform host resets an :class:`InputState`, lets each active
input source (touch overlay, gamepad, keyboard/mouse) write into it, then hands
it to the game bridge. Axes are normalized to ``[-1, 1]`` and clamped; the
bridge maps them onto the engine's player controller (move / strafe / look).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict


# Logical actions the player runtime understands. Kept as plain strings so new
# actions can be added without a schema migration.
ACTION_JUMP = "jump"
ACTION_FIRE = "fire"
ACTION_USE = "use"
ACTION_CROUCH = "crouch"
ACTION_SPRINT = "sprint"
ACTION_PAUSE = "pause"

ALL_ACTIONS = (
    ACTION_JUMP,
    ACTION_FIRE,
    ACTION_USE,
    ACTION_CROUCH,
    ACTION_SPRINT,
    ACTION_PAUSE,
)


class ButtonEdge(Enum):
    """Edge state of a button between the previous and current frame."""

    UP = 0          # not held this frame, not held last frame
    PRESSED = 1     # held this frame, was up last frame (rising edge)
    HELD = 2        # held this frame and last frame
    RELEASED = 3    # up this frame, was held last frame (falling edge)

    @property
    def is_down(self) -> bool:
        return self in (ButtonEdge.PRESSED, ButtonEdge.HELD)


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class InputState:
    """One frame of synthesized input.

    Axes:
      * ``move_x`` / ``move_y`` — left stick. +x = strafe right, +y = forward.
      * ``look_x`` / ``look_y`` — right stick / mouse delta. +x = turn right,
        +y = look up. ``look_*`` are *rate* values (deg/sec scale applied by the
        bridge), not absolute.

    Buttons are tracked as edges so gameplay can distinguish a tap (``PRESSED``)
    from a hold (``HELD``).
    """

    move_x: float = 0.0
    move_y: float = 0.0
    look_x: float = 0.0
    look_y: float = 0.0

    # current-frame held set + previous-frame held set -> derived edges
    _down: Dict[str, bool] = field(default_factory=dict)
    _prev_down: Dict[str, bool] = field(default_factory=dict)

    def begin_frame(self) -> None:
        """Roll button state forward and clear per-frame axes.

        Call once at the top of each frame before any source writes into it.
        """
        self._prev_down = dict(self._down)
        self._down = {a: False for a in ALL_ACTIONS}
        self.move_x = self.move_y = 0.0
        self.look_x = self.look_y = 0.0

    # -- axis accumulation (sources add, then we clamp on read) ---------
    def add_move(self, x: float, y: float) -> None:
        self.move_x = _clamp(self.move_x + x)
        self.move_y = _clamp(self.move_y + y)

    def add_look(self, x: float, y: float) -> None:
        self.look_x += x
        self.look_y += y

    # -- buttons --------------------------------------------------------
    def set_button(self, action: str, down: bool) -> None:
        # OR across sources: touch OR gamepad OR keyboard can hold an action.
        self._down[action] = self._down.get(action, False) or down

    def edge(self, action: str) -> ButtonEdge:
        now = self._down.get(action, False)
        was = self._prev_down.get(action, False)
        if now and not was:
            return ButtonEdge.PRESSED
        if now and was:
            return ButtonEdge.HELD
        if not now and was:
            return ButtonEdge.RELEASED
        return ButtonEdge.UP

    def is_down(self, action: str) -> bool:
        return self._down.get(action, False)

    def just_pressed(self, action: str) -> bool:
        return self.edge(action) == ButtonEdge.PRESSED

    def just_released(self, action: str) -> bool:
        return self.edge(action) == ButtonEdge.RELEASED
